"""Generador de Shorts en español: guion, traducción de búsquedas, Pixabay, subtítulos y render sin red."""

import asyncio
import json
import subprocess

import pytest

from purffle_shorts import ffmpeg, short_es
from tests.conftest import needs_ffmpeg

_WIKIPEDIA_REAL = short_es._wikipedia_extracto


@pytest.fixture(autouse=True)
def _sin_red(monkeypatch):
    def _falla(*a, **k):
        raise RuntimeError("sin red en los tests")
    monkeypatch.setattr(short_es, "_traducir_google", _falla)
    monkeypatch.setattr(short_es, "_traducir_mymemory", _falla)
    monkeypatch.setattr(short_es, "_wikipedia_extracto", lambda tema: None)
    short_es._CACHE_TRADUCCION.clear()


def test_traduccion_sin_red_usa_el_diccionario():
    assert short_es.traducir_al_ingles("los agujeros negros") == "holes black"
    assert short_es.traducir_al_ingles("océano") == "ocean"


def test_traduccion_prefiere_el_servicio(monkeypatch):
    monkeypatch.setattr(short_es, "_traducir_google", lambda t: "Black holes!")
    assert short_es.traducir_al_ingles("agujeros negros") == "black holes"


def test_palabras_clave_ignora_palabras_vacias():
    assert short_es.palabras_clave("¿Sabías que los pulpos tienen tres corazones?", 2) == ["corazones", "pulpos"]


def test_guion_plantilla_en_espanol_y_con_busquedas_en_ingles():
    g = short_es.generar_guion("los volcanes", usar_llm=False)
    assert g.fuente == "plantilla"
    assert all(f.texto for f in g.frases)
    assert "Sígueme" in g.frases[-1].texto
    assert all(f.busqueda_en and f.busqueda_en.isascii() for f in g.frases)
    assert short_es.titulo_final(g).endswith("#Shorts")


def test_guion_wikipedia(monkeypatch):
    extracto = ("El pulpo (Octopoda) es un molusco cefalópodo. Tiene ocho brazos con ventosas. "
                "Posee tres corazones y sangre azul. Es uno de los invertebrados más inteligentes.")
    monkeypatch.setattr(short_es, "_wikipedia_extracto", lambda tema: ("Octopoda", extracto))
    g = short_es.generar_guion("el pulpo", usar_llm=False)
    assert g.fuente == "wikipedia"
    assert "(Octopoda)" not in g.texto and "tres corazones" in g.texto
    assert "Wikipedia" in g.descripcion


def test_pixabay_prefiere_verticales_hd(monkeypatch):
    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            def hit(i, w, h):
                return {"id": i, "duration": 12, "user": "ana", "videos": {
                    "large": {"url": f"https://x/{i}-l.mp4", "width": w * 2, "height": h * 2},
                    "medium": {"url": f"https://x/{i}-m.mp4", "width": w, "height": h},
                    "tiny": {"url": f"https://x/{i}-t.mp4", "width": w // 4, "height": h // 4}}}
            return {"hits": [hit(1, 1280, 720), hit(2, 1080, 1920), hit(3, 540, 960)]}

    pedido = {}

    class Sesion:
        def get(self, url, **kw):
            pedido.update(kw["params"])
            return Resp()

    monkeypatch.setattr(short_es, "http", lambda: Sesion())
    clips = short_es.buscar_pixabay("black hole", "clave")
    assert pedido["q"] == "black hole" and pedido["lang"] == "en"
    assert [c.id for c in clips][:2] == ["2", "3"]          # verticales primero
    assert clips[0].url.endswith("2-m.mp4")                  # el archivo HD más ligero
    assert clips[-1].id == "1" and not clips[-1].vertical


def test_subtitulos_agrupados_sin_huecos(tmp_path):
    P = short_es.Palabra
    palabras = [P("Hola", 0.0, 0.3), P("a", 0.35, 0.4), P("todos", 0.45, 0.8), P("amigos", 0.85, 1.2),
                P("Nueva", 2.0, 2.3), P("frase", 2.35, 2.7)]
    bloques = short_es.agrupar_subtitulos(palabras, 3, duracion_total=3.0)
    assert [b[2] for b in bloques] == ["Hola a todos", "amigos", "Nueva frase"]
    assert bloques[0][1] <= bloques[1][0]
    ass = short_es.escribir_ass(bloques, tmp_path / "s.ass").read_text(encoding="utf-8")
    assert "HOLA A TODOS" in ass and "PlayResY: 1920" in ass
    srt = short_es.escribir_srt(bloques, tmp_path / "s.srt").read_text(encoding="utf-8")
    assert "00:00:00,000 --> " in srt


def test_planificar_tomas_reparte_frases_largas():
    tomas = short_es.planificar_tomas([(0, 3), (3, 14)], 14.3)
    assert tomas[0] == (0, 3)
    assert [i for i, _ in tomas[1:]] == [1, 1, 1]
    assert abs(sum(d for _, d in tomas) - 14.3) < 1e-6


def _voz_falsa(segundos_por_palabra: float):
    async def _edge(texto, voz, velocidad, destino):
        palabras = texto.split()
        pct = short_es._velocidad_pct(velocidad)
        spp = segundos_por_palabra / (1 + pct / 100)
        dur = max(0.5, len(palabras) * spp)
        subprocess.run([ffmpeg.ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"sine=frequency=220:duration={dur:.2f}", "-ar", "24000", str(destino)], check=True)
        return [short_es.Palabra(p.strip(".,¿?¡!"), i * spp, (i + 1) * spp - 0.02) for i, p in enumerate(palabras)]
    return _edge


@needs_ffmpeg
def test_short_completo_sin_red(tmp_path, monkeypatch):
    monkeypatch.setattr(short_es, "_edge_frase", _voz_falsa(0.3))
    video = short_es.crear_short("los volcanes", tmp_path, clave_pixabay="", usar_llm=False)
    info = ffmpeg.probe(video)
    assert 5 < info["duration"] < 60
    out = subprocess.run([ffmpeg.ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "json", str(video)], capture_output=True, text=True)
    stream = json.loads(out.stdout)["streams"][0]
    assert (stream["width"], stream["height"]) == (1080, 1920)
    carpeta = video.parent
    meta = json.loads((carpeta / "metadatos.json").read_text(encoding="utf-8"))
    assert meta["titulo"].endswith("#Shorts") and meta["fuente_guion"] == "plantilla"
    assert (carpeta / "subtitulos.srt").read_text(encoding="utf-8").strip()
    assert not (carpeta / "_temporal").exists()


@needs_ffmpeg
def test_locucion_larga_se_acelera_por_debajo_de_60s(tmp_path, monkeypatch):
    monkeypatch.setattr(short_es, "_edge_frase", _voz_falsa(0.62))   # ~66 s a velocidad normal
    g = short_es.guion_plantilla("las montañas")
    g.frases += [short_es.Frase("Una frase extra con bastantes palabras para alargar la locución un poco más.")] * 3
    loc = short_es.locucion_bajo_60s(g, tmp_path, "es-MX-JorgeNeural", "+0%")
    assert loc.duracion <= short_es.DURACION_MAXIMA
    assert len(loc.tramos) == len(g.frases)


def test_cli_delega_en_short_es(monkeypatch):
    from purffle_shorts import cli
    llamado = {}
    monkeypatch.setattr(short_es, "main", lambda argv: llamado.setdefault("argv", argv) and 0)
    cli.main(["short-es", "--tema", "x"])
    assert llamado["argv"] == ["--tema", "x"]


def test_edge_frase_firma():
    assert asyncio.iscoroutinefunction(short_es._edge_frase)


def test_titulos_candidatos_prefiere_el_singular():
    assert short_es._titulos_candidatos("los pulpos") == ["Los pulpos", "Pulpos", "Pulpo"]
    assert short_es._titulos_candidatos("Marte") == ["Marte"]


def test_wikipedia_prueba_primero_el_singular(monkeypatch):
    pedidos = []

    def paginas(params):
        pedidos.append(params)
        return [{"title": "Octopoda", "extract": "Los pulpos son moluscos."}] if params.get("titles") == "Pulpo" else []
    monkeypatch.setattr(short_es, "_paginas_wikipedia", paginas)
    assert _WIKIPEDIA_REAL("los pulpos") == ("Octopoda", "Los pulpos son moluscos.")
    assert pedidos == [{"titles": "Pulpo"}]           # «Los pulpos» (¿un grupo musical?) ni se consulta


def test_wikipedia_usa_el_buscador_si_no_hay_titulo(monkeypatch):
    def paginas(params):
        return [{"title": "Agujero negro", "extract": "..."}] if "gsrsearch" in params else []
    monkeypatch.setattr(short_es, "_paginas_wikipedia", paginas)
    assert _WIKIPEDIA_REAL("los agujeros negros")[0] == "Agujero negro"


def test_limpiar_wikipedia_quita_secciones():
    assert short_es._limpiar_wikipedia("Texto.\n== Historia ==\nMás texto (nota).\n== Referencias ==\nx") == \
        "Texto. Más texto."


def test_consultas_llevan_el_tema_en_ingles(monkeypatch):
    traducciones = {"los pulpos": "the octopuses", "corazones": "hearts", "brazos": "arms"}
    monkeypatch.setattr(short_es, "_traducir_google", lambda t: traducciones.get(t, t))
    g = short_es.Guion("los pulpos", "t", "d", [], [
        short_es.Frase("¿Sabías esto de los pulpos?"),
        short_es.Frase("Tienen tres corazones."),
        short_es.Frase("Y ocho brazos."),
        short_es.Frase("Sígueme para más.")])
    monkeypatch.setattr(short_es, "guion_con_wikipedia", lambda tema: g)
    out = short_es.generar_guion("los pulpos", usar_llm=False)
    assert [f.busqueda_en for f in out.frases] == ["octopus", "octopus heart", "octopus arm", "octopus"]


def test_descargas_fallidas_reutilizan_clips(tmp_path, monkeypatch):
    clips = [short_es.Clip(str(i), f"https://x/{i}.mp4", 1080, 1920, 10, "ana") for i in range(10)]
    monkeypatch.setattr(short_es, "buscar_pixabay", lambda q, k: clips)
    monkeypatch.setattr(short_es, "PAUSA_ENTRE_DESCARGAS", 0)
    llamadas = []

    def descargar(url, destino):
        llamadas.append(url)
        if len(llamadas) > 1:
            raise RuntimeError("HTTP 429")
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(b"video")
        return destino
    monkeypatch.setattr(short_es, "descargar_clip", descargar)
    b = short_es.BuscadorClips("k", tmp_path, "octopus")
    primero = b.clip_para("octopus", 1)
    assert primero and primero.exists()
    assert b.clip_para("octopus", 2) == primero          # 3 fallos seguidos -> reutiliza
    assert len(llamadas) == 1 + short_es.MAX_FALLOS_SEGUIDOS
    assert b.clip_para("octopus", 3) == primero          # ya no intenta descargar más
    assert len(llamadas) == 1 + short_es.MAX_FALLOS_SEGUIDOS


def test_descargar_clip_respeta_retry_after(tmp_path, monkeypatch):
    import time
    esperas = []
    monkeypatch.setattr(time, "sleep", esperas.append)

    class Resp:
        def __init__(self, code):
            self.status_code = code
            self.headers = {"Retry-After": "7"} if code == 429 else {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def raise_for_status(self):
            pass

        def iter_content(self, n):
            return [b"abc"]

    respuestas = [Resp(429), Resp(200)]

    class Sesion:
        def get(self, url, **kw):
            return respuestas.pop(0)
    monkeypatch.setattr(short_es, "http", lambda: Sesion())
    destino = short_es.descargar_clip("https://x/1.mp4", tmp_path / "c.mp4")
    assert destino.read_bytes() == b"abc" and esperas == [7.0]


def test_singular_en():
    assert short_es.singular_en("octopuses limbs stories glass people") == "octopus limb story glass person"
    assert short_es.singular_en("black holes") == "black hole"


def test_tomas_sin_clip_reutilizan_los_descargados(tmp_path, monkeypatch):
    """La primera consulta no da resultados: su toma usa un clip encontrado después, no un fondo liso."""
    usados = []
    monkeypatch.setattr(short_es, "preparar_toma", lambda clip, dur, destino, color: usados.append(clip) or destino)
    monkeypatch.setattr(short_es.ffmpeg, "run", lambda *a, **k: "")

    class Falso:
        def __init__(self, *a):
            self.descargados, self.autores = [], set()

        def clip_para(self, q, n):
            if q == "nada":
                return None
            self.descargados.append(tmp_path / f"{q}.mp4")
            return self.descargados[-1]

        def _reutilizar(self, n):
            return self.descargados[n % len(self.descargados)]
    monkeypatch.setattr(short_es, "BuscadorClips", Falso)
    g = short_es.Guion("x", "t", "d", [], [short_es.Frase("a b", "nada"), short_es.Frase("c d", "octopus")])
    loc = short_es.Locucion(tmp_path / "voz.wav", 4.0, [], [(0, 2), (2, 4)])
    short_es.renderizar(g, loc, tmp_path, tmp_path / "s.mp4", "clave")
    assert usados == [tmp_path / "octopus.mp4"] * 2
