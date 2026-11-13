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
    assert "enlace de mi perfil" in g.frases[-1].texto
    assert all(f.busqueda_en and f.busqueda_en.isascii() for f in g.frases)
    assert short_es.titulo_final(g).endswith("#Shorts")


def test_guion_wikipedia(monkeypatch):
    extracto = ("El pulpo (Octopoda) es un molusco cefalópodo. Tiene ocho brazos con ventosas. "
                "Posee tres corazones y sangre azul. Es uno de los invertebrados más inteligentes.")
    monkeypatch.setattr(short_es, "_wikipedia_extracto", lambda tema: ("Octopoda", extracto))
    g = short_es.generar_guion("el pulpo", usar_llm=False, usar_wikipedia=True)
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
    out = short_es.generar_guion("los pulpos", usar_llm=False, usar_wikipedia=True)
    # el cierre sin enlace se completa con el CTA del canal (última toma: el tema)
    assert [f.busqueda_en for f in out.frases] == ["octopus", "octopus heart", "octopus arm", "octopus", "person smiling smartphone"]


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


# ------------------------------------------------------------------ cliente ideal y reglas del canal
def _guion_llm(**kw):
    datos = {"frase_clave": "ganar dinero con IA", "titulo": "Ganar dinero con IA 2026 solo con tu celular",
             "descripcion": "Ganar dinero con IA sin humo.\nSi buscas ganar dinero con IA, empieza aquí.",
             "hashtags": ["#GanarDineroConIA", "emprendimiento"],
             "frases": [{"texto": "Solo con tu celular y una hora al día puedes empezar esto.",
                         "busqueda_en": "person using smartphone"},
                        {"texto": " ".join(["paso"] * 100) + ".", "busqueda_en": "notebook"},
                        {"texto": "Si quieres el mapa paso a paso, sin humo, revisa el enlace de mi perfil.",
                         "busqueda_en": "smiling"}]}
    datos.update(kw)
    return datos


def test_prompt_incluye_cliente_ideal_y_reglas():
    sistema = short_es.SISTEMA_LLM
    for texto in ("25 a 45 años", "Colombia", "1 a 2 horas al día", "antihumo", "PROHIBIDO prometer riqueza",
                  '"tú"', "lujos"):
        assert texto in sistema
    prompt = short_es._prompt_llm("cómo empezar con 1 hora al día")
    for texto in ("Gancho (0-3 s)", "Si yo tuviera que", "La dura verdad sobre", "últimos 8 s",
                  "enlace de mi perfil", "«ingresos extra desde casa»", "«ganar dinero con IA»",
                  "«emprender sin dinero»", "2026", "línea 1 y se repite en la línea 2", "EJEMPLO DEL FORMATO"):
        assert texto in prompt
    assert "Franquicia IA" not in prompt                        # sin bloque comercial si el tema no lo pide


def test_prompt_franquicia_exige_transparencia():
    prompt = short_es._prompt_llm("qué es la Franquicia IA y si vale la pena")
    for texto in ("97 dólares", "50 %", "48,50 dólares", "sin ventas no hay ingresos",
                  "tú, el creador, ganas una comisión"):
        assert texto in prompt


def test_llm_recibe_correcciones_y_se_ajusta(monkeypatch):
    from purffle_shorts import llm as llm_mod
    prompts = []

    class LLMFalso:
        def __init__(self, settings):
            pass

        def complete_json(self, sistema, prompt, **kw):
            prompts.append(prompt)
            return _guion_llm(titulo="Mi historia") if len(prompts) == 1 else _guion_llm()
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setattr(llm_mod, "LLM", LLMFalso)
    g = short_es.guion_con_llm("ganar dinero con IA")
    assert len(prompts) == 2 and "debe empezar con la frase clave" in prompts[1]
    assert g.titulo.startswith("Ganar dinero con IA") and g.frase_clave == "ganar dinero con IA"
    assert short_es.problemas_del_guion(g) == []


def test_franquicia_sin_transparencia_se_completa_y_no_se_recorta():
    g = short_es._guion_desde_json("la Franquicia IA", _guion_llm())
    assert "Franquicia IA cuesta 97" not in g.texto
    g = short_es.ajustar_a_reglas(g)
    assert short_es._menciona_transparencia(g.texto)
    assert g.frases[-2].texto == short_es.FRASE_TRANSPARENCIA_FRANQUICIA
    assert g.frases[-2].fija and g.frases[0].fija and g.frases[-1].fija
    assert short_es.contar_palabras(g.texto) <= short_es.PALABRAS_OBJETIVO[1]   # el relleno largo se recortó


def test_cierre_sin_enlace_recibe_el_cta():
    datos = _guion_llm()
    datos["frases"][-1]["texto"] = "Gracias por ver."
    g = short_es.ajustar_a_reglas(short_es._guion_desde_json("x", datos))
    assert g.frases[-1].texto == short_es.CTA_POR_DEFECTO


def test_detecta_promesas_prohibidas():
    datos = _guion_llm()
    datos["frases"][1]["texto"] = "Con esto vas a hacerte rico y comprarte un Lamborghini " + "ya " * 95
    problemas = short_es.problemas_del_guion(short_es._guion_desde_json("x", datos))
    assert any("Promete riqueza" in p for p in problemas)


def test_titulo_y_descripcion_seo():
    g = short_es.ajustar_a_reglas(short_es._guion_desde_json("x", _guion_llm(
        titulo="Tu primer paso solo con el celular", descripcion="Un video corto.")))
    assert g.titulo.startswith("Ganar dinero con IA: ")
    d = short_es.descripcion_final(g, {"ana"}).splitlines()
    assert "ganar dinero con ia" in d[0].lower() and "ganar dinero con ia" in d[1].lower()
    texto = "\n".join(d)
    assert short_es.NOTA_TRANSPARENCIA in texto and "comisión sin costo extra" in texto
    assert "Pixabay (ana)" in texto and "#ganardineroconia" in texto and texto.rstrip().endswith("#shorts")


def test_recorte_respeta_frases_fijas(tmp_path, monkeypatch):
    monkeypatch.setattr(short_es, "_edge_frase", _voz_falsa(0.62))
    g = short_es.ajustar_a_reglas(short_es.guion_plantilla("la Franquicia IA"))
    obligatorias = [f.texto for f in g.frases if f.fija]
    short_es.locucion_bajo_60s(g, tmp_path, "es-MX-JorgeNeural", "+0%")
    assert all(t in [f.texto for f in g.frases] for t in obligatorias)


def test_prompt_es_corto():
    """Un prompt corto y con ejemplo se sigue mejor: sistema + usuario por debajo de 4.500 caracteres."""
    for tema in ("cómo empezar con 1 hora al día", "la Franquicia IA"):
        assert len(short_es.SISTEMA_LLM) + len(short_es._prompt_llm(tema)) < 4500


def test_titulo_no_termina_en_conector():
    assert short_es.recortar_titulo("Ganar dinero con IA 2026: la Franquicia IA: qué es, cuánto cuesta y para "
                                    "quién no es ni nunca será para todos", 70) == \
        "Ganar dinero con IA 2026: la Franquicia IA: qué es, cuánto cuesta"


def test_plantilla_franquicia_cabe_en_el_rango():
    g = short_es.ajustar_a_reglas(short_es.guion_plantilla("la Franquicia IA: qué es, cuánto cuesta y para quién no es"))
    assert short_es.contar_palabras(g.texto) <= short_es.PALABRAS_OBJETIVO[1]
    assert short_es._menciona_transparencia(g.texto) and "enlace de mi perfil" in g.frases[-1].texto
    assert short_es.titulo_final(g) == "Ganar dinero con IA 2026: la Franquicia IA, sin humo #Shorts"


def test_groq_es_respaldo_de_gemini(monkeypatch):
    from purffle_shorts.config import Settings
    from purffle_shorts.llm import LLM
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("GROQ_API_KEY", "q")
    monkeypatch.setenv("LLM_FALLBACKS", "groq")
    assert [str(p).split(":")[0] for p in LLM(Settings.from_env()).providers] == ["gemini", "groq"]
    monkeypatch.delenv("GROQ_API_KEY")               # sin la clave de Groq, Gemini sigue funcionando solo
    assert [str(p).split(":")[0] for p in LLM(Settings.from_env()).providers] == ["gemini"]


def test_elegir_modelo_groq():
    disponibles = ["whisper-large-v3", "meta-llama/llama-prompt-guard-2-86m", "openai/gpt-oss-120b",
                   "meta-llama/llama-4-maverick-17b-128e-instruct", "playai-tts"]
    assert short_es.elegir_modelo_groq(disponibles) == "meta-llama/llama-4-maverick-17b-128e-instruct"
    assert short_es.elegir_modelo_groq(["openai/gpt-oss-120b", "whisper-large-v3"]) == "openai/gpt-oss-120b"
    assert short_es.elegir_modelo_groq(["whisper-large-v3"]) is None


def test_groq_model_se_usa_en_el_respaldo(monkeypatch):
    from purffle_shorts.config import Settings
    from purffle_shorts.llm import LLM
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("GROQ_API_KEY", "q")
    monkeypatch.setenv("LLM_FALLBACKS", "groq")
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-120b")
    assert str(LLM(Settings.from_env()).providers[1]) == "groq:openai/gpt-oss-120b"


def test_configurar_modelo_groq_consulta_la_cuenta(monkeypatch):
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"id": "whisper-large-v3"}, {"id": "openai/gpt-oss-120b", "active": True}]}

    class Sesion:
        def get(self, url, **kw):
            assert url.endswith("/models") and kw["headers"]["Authorization"] == "Bearer q"
            return Resp()
    monkeypatch.setattr(short_es, "http", lambda: Sesion())
    monkeypatch.setenv("GROQ_API_KEY", "q")
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    short_es._configurar_modelo_groq()
    import os
    assert os.environ["GROQ_MODEL"] == "openai/gpt-oss-120b"
    monkeypatch.delenv("GROQ_MODEL")


def test_guion_real_de_groq_sin_duplicados():
    """Lo que escribió Groq en la ejecución 8: transparencia incompleta, CTA en medio y una frase después."""
    frases = [
        "Si tienes 1 hora al día y solo tu celular, prueba la franquicia IA.",
        "Los tutoriales están desordenados y temes perder dinero, pero la franquicia IA solo cuesta 97 dólares.",
        "Te enseña embudos, pauta y automatización con IA, y cada venta genera el 50 %, unos 48,50 dólares, con tu "
        "propio enlace.",
        "Sin ventas no hay ingresos, así que el primer paso es crear tu enlace de afiliado y compartirlo en tus redes.",
        "Usa la herramienta gratuita de generación de links que la plataforma ofrece y prueba un embudo sencillo hoy.",
        "Si quieres el mapa paso a paso con IA, sin humo ni promesas falsas, revisa el enlace de mi perfil.",
        "Empieza ahora y ve los resultados poco a poco.",
    ]
    datos = {"frase_clave": "ganar dinero con IA",
             "titulo": "ganar dinero con IA 2026: descubre la franquicia IA sin invertir",
             "descripcion": "x", "hashtags": [], "frases": [{"texto": t, "busqueda_en": "x"} for t in frases]}
    tema = "la Franquicia IA: qué es, cuánto cuesta y para quién no es"
    g = short_es.ajustar_a_reglas(short_es._guion_desde_json(tema, datos))
    textos = [f.texto for f in g.frases]
    assert textos[-1] == frases[5]                                    # un solo CTA, al final
    assert "Empieza ahora" not in " ".join(textos)
    assert sum("97" in t for t in textos) == 1                        # el precio no se repite
    assert textos.count(short_es.FRASE_TRANSPARENCIA_FRANQUICIA) == 1
    assert short_es.contar_palabras(g.texto) <= short_es.PALABRAS_OBJETIVO[1]
    assert g.titulo.startswith("Ganar dinero con IA") and "sin invertir" not in g.titulo
    # Al quitar las frases parciales queda más corto (~99 palabras, ~38 s): solo falta la duración ideal.
    assert [p for p in short_es.problemas_del_guion(g) if "palabras" not in p] == []


# ------------------------------------------------------------------ temas del día y subida a YouTube
def test_tema_del_dia_rota_y_salta_comentarios(tmp_path):
    import datetime as dt
    f = tmp_path / "temas.txt"
    f.write_text("# comentario\n\nuno\ndos\n  # otro\ntres\n", encoding="utf-8")
    assert short_es.leer_temas(f) == ["uno", "dos", "tres"]
    dia = dt.date(2026, 11, 13)
    vistos = [short_es.tema_del_dia(f, dia + dt.timedelta(days=n)) for n in range(6)]
    assert sorted(vistos[:3]) == ["dos", "tres", "uno"] and vistos[3:] == vistos[:3]


def test_el_archivo_de_temas_del_repo_es_valido():
    from pathlib import Path
    temas = short_es.leer_temas(Path(__file__).resolve().parents[1] / "temas_shorts.txt")
    assert len(temas) >= 10
    assert sum(bool(short_es.FRANQUICIA_RE.search(t)) for t in temas) == 1      # el producto, una vez por ciclo


def test_cli_usa_el_tema_del_dia(tmp_path, monkeypatch):
    f = tmp_path / "temas.txt"
    f.write_text("tema único\n", encoding="utf-8")
    usados = {}

    def crear(tema, salida, **kw):
        usados["tema"] = tema
        return tmp_path / "x" / "short.mp4"
    monkeypatch.setattr(short_es, "crear_short", crear)
    assert short_es.main(["--tema", "", "--temas", str(f)]) == 0
    assert usados["tema"] == "tema único"


def _carpeta_generada(tmp_path):
    c = tmp_path / "short"
    c.mkdir()
    (c / "short.mp4").write_bytes(b"mp4")
    (c / "titulo.txt").write_text("Ganar dinero con IA 2026: tu primer paso #Shorts\n", encoding="utf-8")
    (c / "descripcion.txt").write_text("Ganar dinero con IA...\n\n#shorts\n", encoding="utf-8")
    (c / "subtitulos.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHola\n", encoding="utf-8")
    (c / "metadatos.json").write_text(json.dumps({"tema": "t", "frase_clave": "ganar dinero con IA",
                                                  "hashtags": ["ingresosextra", "emprendimiento"]}),
                                      encoding="utf-8")
    return c


def test_subir_a_youtube_privado_con_subtitulos(tmp_path, monkeypatch):
    from purffle_shorts import youtube
    llamadas = {}

    def upload(settings, video, body):
        llamadas["body"], llamadas["video"] = body, video
        return {"id": "abc123"}
    monkeypatch.setattr(youtube, "upload", upload)
    monkeypatch.setattr(youtube, "upload_captions",
                        lambda s, vid, srt, lang, name="": llamadas.setdefault("subs", (vid, srt.name, lang)))
    c = _carpeta_generada(tmp_path)
    assert short_es.subir_a_youtube(c, "privado") == "abc123"
    body = llamadas["body"]
    assert body["status"]["privacyStatus"] == "private" and body["status"]["containsSyntheticMedia"] is True
    assert body["snippet"]["title"].startswith("Ganar dinero con IA") and body["snippet"]["defaultLanguage"] == "es"
    assert body["snippet"]["tags"][:2] == ["ganar dinero con IA", "ingresosextra"]
    assert llamadas["subs"] == ("abc123", "subtitulos.srt", "es")
    meta = json.loads((c / "metadatos.json").read_text(encoding="utf-8"))
    assert meta["youtube_url"] == "https://youtube.com/shorts/abc123"


def test_cli_subir_carpeta(tmp_path, monkeypatch):
    pedido = {}
    monkeypatch.setattr(short_es, "subir_a_youtube", lambda c, p: pedido.update(c=c, p=p) or "id")
    assert short_es.main(["--subir-carpeta", str(tmp_path), "--subir", "oculto"]) == 0
    assert pedido == {"c": str(tmp_path), "p": "oculto"}


def test_subir_sin_autorizacion_falla_con_mensaje(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_TOKEN_FILE", str(tmp_path / "no-existe.json"))
    c = _carpeta_generada(tmp_path)
    assert short_es.main(["--subir-carpeta", str(c)]) == 1


# ------------------------------------------------------------------ guion propio
GUION_PROPIO = """# comentario
Tema: ingresos extra desde casa
Título: Mi título exacto
Si tienes una hora al día, empieza por aquí.
El error es saltar de tutorial en tutorial. Termina uno antes de empezar otro.
Revisa el enlace de mi perfil si quieres el mapa completo."""


def test_leer_guion_con_cabeceras_y_separadores():
    tema, titulo, frases = short_es.leer_guion(GUION_PROPIO)
    assert tema == "ingresos extra desde casa" and titulo == "Mi título exacto"
    assert frases == ["Si tienes una hora al día, empieza por aquí.", "El error es saltar de tutorial en tutorial.",
                      "Termina uno antes de empezar otro.",
                      "Revisa el enlace de mi perfil si quieres el mapa completo."]
    assert short_es.leer_guion("Uno dos | tres cuatro")[2] == ["Uno dos", "tres cuatro"]


def test_guion_propio_no_se_modifica_y_respeta_el_titulo():
    g = short_es.guion_desde_texto(GUION_PROPIO, usar_llm=False)
    assert [f.texto for f in g.frases] == short_es.leer_guion(GUION_PROPIO)[2]
    assert all(f.fija for f in g.frases) and g.fuente == "propio"
    assert g.titulo == "Mi título exacto" and short_es.titulo_final(g) == "Mi título exacto #Shorts"
    assert all(f.busqueda_en and f.busqueda_en.isascii() for f in g.frases)
    d = short_es.descripcion_final(g, set())
    assert short_es.NOTA_TRANSPARENCIA in d and g.frase_clave.lower() in d.splitlines()[0].lower()


def test_guion_propio_franquicia_no_agrega_nada_pero_avisa(caplog):
    texto = "Tema: la Franquicia IA\nLa Franquicia IA te enseña a vender.\nRevisa el enlace de mi perfil."
    with caplog.at_level("WARNING", logger="purffle.short_es"):
        g = short_es.guion_desde_texto(texto, usar_llm=False)
    assert [f.texto for f in g.frases] == ["La Franquicia IA te enseña a vender.", "Revisa el enlace de mi perfil."]
    assert "Falta la transparencia" in caplog.text and "no se cambió" in caplog.text
    assert g.titulo.startswith("Ganar dinero con IA 2026")                       # título creado por el sistema


def test_guion_propio_usa_metadatos_del_llm(monkeypatch):
    class LLMFalso:
        def complete_json(self, sistema, prompt, **kw):
            assert "NO se cambia" in prompt and "1. Si tienes" in prompt
            return {"frase_clave": "ingresos extra desde casa", "titulo": "Ingresos extra desde casa: tu paso",
                    "descripcion": "Ingresos extra desde casa hoy.\nSi buscas ingresos extra desde casa, mira.",
                    "hashtags": ["ingresosextra"], "busquedas": ["phone night", "notebook", "desk", "smile"]}
    monkeypatch.setattr(short_es, "_crear_llm", lambda: LLMFalso())
    g = short_es.guion_desde_texto(GUION_PROPIO.replace("Título: Mi título exacto\n", ""))
    assert g.titulo == "Ingresos extra desde casa: tu paso"
    assert [f.busqueda_en for f in g.frases] == ["phone night", "notebook", "desk", "smile"]
    assert g.frases[0].texto == "Si tienes una hora al día, empieza por aquí."


def test_guion_propio_vacio_falla():
    with pytest.raises(ValueError):
        short_es.guion_desde_texto("# solo comentarios\nTema: algo", usar_llm=False)


@needs_ffmpeg
def test_guion_propio_largo_se_acelera_pero_no_se_recorta(tmp_path, monkeypatch):
    monkeypatch.setattr(short_es, "_edge_frase", _voz_falsa(0.62))
    texto = "\n".join(["Una frase de prueba con bastantes palabras para alargar el guion del video."] * 9)
    g = short_es.guion_desde_texto(texto, usar_llm=False)
    loc = short_es.locucion_bajo_60s(g, tmp_path, "es-MX-JorgeNeural", "+0%")
    assert len(g.frases) == 9 and loc.duracion <= short_es.DURACION_MAXIMA


@needs_ffmpeg
def test_guion_propio_demasiado_largo_da_un_error_claro(tmp_path, monkeypatch):
    monkeypatch.setattr(short_es, "_edge_frase", _voz_falsa(0.62))
    texto = "\n".join(["Una frase de prueba con bastantes palabras para alargar el guion del video."] * 14)
    g = short_es.guion_desde_texto(texto, usar_llm=False)
    with pytest.raises(RuntimeError, match="acorta el guion"):
        short_es.locucion_bajo_60s(g, tmp_path, "es-MX-JorgeNeural", "+0%")


def test_cli_guion_archivo_y_texto(tmp_path, monkeypatch):
    usados = []
    monkeypatch.setattr(short_es, "crear_short",
                        lambda tema, salida, **kw: usados.append((tema, kw["guion_propio"])) or tmp_path / "s.mp4")
    f = tmp_path / "g.txt"
    f.write_text("Hola mundo de prueba.", encoding="utf-8")
    assert short_es.main(["--guion", str(f)]) == 0
    assert short_es.main(["--guion-texto", "Uno dos | tres cuatro", "--tema", "mi tema"]) == 0
    assert usados == [("", "Hola mundo de prueba."), ("mi tema", "Uno dos | tres cuatro")]


def test_el_guion_de_ejemplo_del_repo_es_valido():
    from pathlib import Path
    texto = (Path(__file__).resolve().parents[1] / "guiones" / "ejemplo.txt").read_text(encoding="utf-8")
    g = short_es.guion_desde_texto(texto, usar_llm=False)
    assert short_es.problemas_del_guion(g) == [] or all("palabras" in p for p in short_es.problemas_del_guion(g))
    assert short_es.contar_palabras(g.texto) <= short_es.PALABRAS_OBJETIVO[1]
