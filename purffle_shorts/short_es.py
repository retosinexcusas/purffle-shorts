"""Generador de YouTube Shorts en español (9:16, menos de 60 s) solo con herramientas gratuitas.

Flujo:  tema -> guion en español -> voz edge-tts (es-MX-JorgeNeural) -> clips verticales HD de Pixabay
(buscados con palabras clave traducidas al inglés) -> subtítulos en español quemados con FFmpeg -> .mp4

Uso:
    python -m purffle_shorts.short_es --tema "los agujeros negros"
    purffle-shorts short-es "los agujeros negros"

Variables de entorno:
    PIXABAY_API_KEY   clave gratuita de https://pixabay.com/api/docs/ (clips de fondo)
    VOZ_ES            voz de edge-tts (por defecto es-MX-JorgeNeural)
    VELOCIDAD_VOZ     velocidad de la voz, p. ej. +10% (por defecto +8%)
    GEMINI_API_KEY / GROQ_API_KEY   opcionales (niveles gratuitos): un LLM escribe el guion para el cliente
                      ideal del canal (ver SISTEMA_LLM). Sin ellas se usa una plantilla antihumo del canal.

Todo el texto que ve el público (guion, título, descripción y subtítulos) se genera en español. Solo las
búsquedas en Pixabay se hacen en inglés, porque ahí sus resultados son mucho mejores.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import random
import re
import shutil
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import ffmpeg
from .utils import clean_narration, http, slugify, truncate_words, with_retries

log = logging.getLogger("purffle.short_es")

ANCHO, ALTO, FPS = 1080, 1920, 30
DURACION_MAXIMA = 58.5          # margen bajo los 60 s que YouTube exige a un Short
VOZ_POR_DEFECTO = "es-MX-JorgeNeural"
VELOCIDAD_POR_DEFECTO = "+8%"
VELOCIDAD_MAXIMA = 35           # % — más rápido ya no se entiende bien
PALABRAS_OBJETIVO = (115, 140)  # ~45-55 s de locución con es-MX-JorgeNeural a +8 %
MAX_SEGUNDOS_POR_TOMA = 5.0     # las frases largas se cubren con varios clips (ritmo de Short)
PALABRAS_POR_SUBTITULO = 3

# ------------------------------------------------------------------------------------------ texto
STOPWORDS_ES = set("""
a al algo algún alguna algunas alguno algunos ante antes aquel aquella aquello aquí así aun aunque bajo bien
cada casi como cómo con contra cual cuál cuales cuando cuándo cuanto de del desde donde dónde dos durante e el
él ella ellas ellos en entre era eran es esa esas ese eso esos esta está están estas este esto estos fue fueron
ha había han hasta hay la las le les lo los más me mi mis mucho muy nada ni no nos o otra otras otro otros para
pero poco por porque qué que quien quién se sea ser si sí sido sin sobre su sus también tan tanto te tiene
tienen todo todos tu tú un una uno unos unas y ya yo puede pueden cuál sabías sabias datos dato curioso
curiosos increíble verdad realmente hace hacer cosa cosas parte vez veces años año gran grande mismo misma
según tras solo sólo cada nuestro nuestra vida mundo sígueme sigueme más
""".split())

# Último recurso si no hay red para traducir: palabras frecuentes en temas de Shorts.
DICCIONARIO_ES_EN = {
    "agua": "water", "mar": "sea", "océano": "ocean", "oceano": "ocean", "cielo": "sky", "espacio": "space",
    "estrellas": "stars", "estrella": "star", "planeta": "planet", "planetas": "planets", "tierra": "earth",
    "luna": "moon", "sol": "sun", "universo": "universe", "galaxia": "galaxy", "agujero": "hole",
    "negro": "black", "negros": "black", "agujeros": "holes", "animal": "animal", "animales": "animals",
    "perro": "dog", "perros": "dogs", "gato": "cat", "gatos": "cats", "pájaro": "bird", "pájaros": "birds",
    "aves": "birds", "pez": "fish", "peces": "fish", "tiburón": "shark", "tiburones": "sharks",
    "pulpo": "octopus", "ballena": "whale", "ballenas": "whales", "selva": "jungle", "bosque": "forest",
    "montaña": "mountain", "montañas": "mountains", "ciudad": "city", "ciudades": "cities", "historia": "history",
    "antiguo": "ancient", "antigua": "ancient", "egipto": "egypt", "pirámides": "pyramids", "roma": "rome",
    "guerra": "war", "dinero": "money", "economía": "economy", "tecnología": "technology",
    "inteligencia": "intelligence", "artificial": "artificial", "robot": "robot", "robots": "robots",
    "computadora": "computer", "teléfono": "phone", "cerebro": "brain", "cuerpo": "body", "humano": "human",
    "corazón": "heart", "salud": "health", "comida": "food", "café": "coffee", "fuego": "fire",
    "volcán": "volcano", "volcanes": "volcanoes", "tormenta": "storm", "lluvia": "rain", "nieve": "snow",
    "hielo": "ice", "desierto": "desert", "río": "river", "lago": "lake", "playa": "beach", "noche": "night",
    "día": "day", "luz": "light", "energía": "energy", "coche": "car", "coches": "cars", "auto": "car",
    "avión": "airplane", "tren": "train", "barco": "ship", "deporte": "sport", "fútbol": "soccer",
    "música": "music", "arte": "art", "ciencia": "science", "misterio": "mystery", "misterios": "mysteries",
    "naturaleza": "nature", "flores": "flowers", "árbol": "tree", "árboles": "trees", "dinosaurios": "dinosaurs",
    "insectos": "insects", "abejas": "bees", "hormigas": "ants", "niños": "children", "familia": "family",
    "trabajo": "work", "oficina": "office", "escuela": "school", "libro": "book", "libros": "books",
    "sueño": "sleep", "dormir": "sleep", "correr": "running", "ejercicio": "exercise", "mente": "mind",
}

_PALABRA = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9]+")


def sin_acentos(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def palabras_clave(texto: str, limite: int = 3) -> list[str]:
    """Las palabras con más carga visual de una frase (las más largas que no son vacías)."""
    vistas, salida = set(), []
    candidatas = [p for p in _PALABRA.findall(texto.lower()) if p not in STOPWORDS_ES and len(p) > 3
                  and not p.isdigit()]
    for p in sorted(candidatas, key=len, reverse=True):
        if p not in vistas:
            vistas.add(p)
            salida.append(p)
        if len(salida) >= limite:
            break
    return salida


def dividir_frases(texto: str) -> list[str]:
    partes = re.split(r"(?<=[.!?…])\s+", clean_narration(texto))
    return [p.strip() for p in partes if len(_PALABRA.findall(p)) >= 2]


def contar_palabras(texto: str) -> int:
    return len(_PALABRA.findall(texto))


# ------------------------------------------------------------------------------------------ traducción
def _traducir_google(texto: str) -> str:
    r = http().get("https://translate.googleapis.com/translate_a/single", timeout=15,
                   params={"client": "gtx", "sl": "es", "tl": "en", "dt": "t", "q": texto})
    r.raise_for_status()
    return "".join(seg[0] for seg in r.json()[0] if seg and seg[0]).strip()


def _traducir_mymemory(texto: str) -> str:
    r = http().get("https://api.mymemory.translated.net/get", timeout=15,
                   params={"q": texto, "langpair": "es|en"})
    r.raise_for_status()
    out = (r.json().get("responseData") or {}).get("translatedText") or ""
    if "MYMEMORY WARNING" in out.upper():
        raise RuntimeError(out)
    return out.strip()


def _traducir_diccionario(texto: str) -> str:
    palabras = [p for p in _PALABRA.findall(texto.lower()) if p not in STOPWORDS_ES]
    return " ".join(DICCIONARIO_ES_EN.get(p) or DICCIONARIO_ES_EN.get(sin_acentos(p)) or sin_acentos(p)
                    for p in palabras)


_CACHE_TRADUCCION: dict[str, str] = {}
STOPWORDS_EN = set("the a an of and or to in on for with about you your is are was were its it this that "
                   "these those their they we our my know learned new".split())


def traducir_al_ingles(texto: str) -> str:
    """Traduce palabras clave del español al inglés para buscar en Pixabay (uso interno, nunca se muestra)."""
    texto = texto.strip()
    if not texto:
        return ""
    if texto in _CACHE_TRADUCCION:
        return _CACHE_TRADUCCION[texto]
    resultado = ""
    for traductor in (_traducir_google, _traducir_mymemory):
        try:
            resultado = traductor(texto)
            if resultado:
                break
        except Exception as e:  # sin red o servicio caído: probamos el siguiente
            log.debug("Traductor %s falló: %s", traductor.__name__, e)
    if not resultado:
        resultado = _traducir_diccionario(texto)
    resultado = re.sub(r"[^A-Za-z0-9 ]+", " ", sin_acentos(resultado)).strip().lower()
    resultado = re.sub(r"\s+", " ", resultado)
    _CACHE_TRADUCCION[texto] = resultado
    return resultado


_PLURALES_IRREGULARES = {"people": "person", "children": "child", "men": "man", "women": "woman", "mice": "mouse",
                         "teeth": "tooth", "feet": "foot", "geese": "goose", "fish": "fish", "species": "species",
                         "series": "series", "news": "news", "physics": "physics", "mathematics": "mathematics"}


def singular_en(texto: str) -> str:
    """Pixabay etiqueta en singular: "octopuses limbs" -> "octopus limb"."""
    def una(w: str) -> str:
        if w in _PLURALES_IRREGULARES:
            return _PLURALES_IRREGULARES[w]
        if len(w) <= 3 or w.endswith(("ss", "us", "is", "ous")):
            return w
        if w.endswith("ies"):
            return w[:-3] + "y"
        if w.endswith(("ses", "xes", "ches", "shes", "zes")):
            return w[:-2]
        if w.endswith("s"):
            return w[:-1]
        return w
    return " ".join(una(w) for w in texto.split())


def consulta_tema(tema: str) -> str:
    """El tema en inglés, en singular y sin artículos: «los pulpos» -> "octopus"."""
    palabras = [w for w in traducir_al_ingles(tema).split() if w not in STOPWORDS_EN]
    return singular_en(" ".join(palabras[:3])) or sin_acentos(tema.lower())


# ------------------------------------------------------------------------------------------ guion
@dataclass
class Frase:
    texto: str
    busqueda_en: str = ""        # consulta en inglés para Pixabay (interna)
    fija: bool = False           # no se recorta al ajustar la duración (gancho, transparencia, cierre)


@dataclass
class Guion:
    tema: str
    titulo: str
    descripcion: str
    hashtags: list[str]
    frases: list[Frase]
    fuente: str = "plantilla"    # llm | wikipedia | plantilla
    frase_clave: str = ""        # búsqueda real de YouTube que encabeza el título y la descripción

    @property
    def texto(self) -> str:
        return " ".join(f.texto for f in self.frases)


# --- Cliente ideal y reglas del canal ---------------------------------------------------------------
# Búsquedas reales de YouTube (sugerencias del buscador desde Colombia, 11/11/2026).
PALABRAS_CLAVE_SEO = [   # las primeras _PALABRAS_CLAVE_PROMPT se le ofrecen al modelo
    "ingresos extra desde casa", "ganar dinero con IA", "emprender sin dinero",
    "como ganar dinero con IA sin invertir", "como emprender sin dejar de trabajar",
    "trabajar desde casa por internet colombia", "productos digitales como empezar",
    "como vender en instagram desde cero", "como conseguir clientes para mi negocio", "gurus de internet",
    "ingresos extra 2026", "generar ingresos extras por internet", "ganar dinero con IA 2026",
    "emprender sin dinero 2026", "emprender desde casa sin dinero", "ingresos pasivos con IA",
    "productos digitales con IA",
]
HASHTAGS_BASE = ["ingresosextra", "emprendimiento", "emprendedores", "ganardineroconia", "emprendersindinero",
                 "emprendedoradigital", "productosdigitales", "ingresospasivos", "emprendersinmiedo"]
FRANQUICIA_RE = re.compile(r"franquicia\s+(?:de\s+)?(?:ia|i\.\s*a\.?|inteligencia\s+artificial)\b", re.I)
CTA_POR_DEFECTO = ("Si quieres el mapa paso a paso con inteligencia artificial, sin humo ni promesas falsas, "
                   "revisa el enlace de mi perfil.")
FRASE_TRANSPARENCIA_FRANQUICIA = (
    "Te lo digo claro: la Franquicia IA cuesta 97 dólares, enseña embudos, pauta y automatización con IA, "
    "ganas el 50 por ciento, unos 48,50 dólares, por cada venta con tu enlace, sin ventas no hay ingresos, "
    "y yo gano una comisión si entras con el mío.")
NOTA_TRANSPARENCIA = ("Transparencia: en este canal comparto herramientas gratuitas y enlaces de afiliado. Si compras "
                      "a través de ellos, puedo ganar una comisión sin costo extra para ti.")
PROMESAS_PROHIBIDAS = re.compile(
    r"\b(?:hazte rico|hacerte rico|volverte rico|dinero f[aá]cil|riqueza r[aá]pida|garantizad[oa]s?|"
    r"garantiza|sin esfuerzo|lamborghini|ferrari|yate|mansi[oó]n|millonario en)\b", re.I)

SISTEMA_LLM = """Eres guionista de YouTube Shorts de un canal antihumo de emprendimiento digital. Escribes solo \
en español latinoamericano natural (con cercanía colombiana); únicamente 'busqueda_en' va en inglés. Respondes solo \
con JSON válido.

AUDIENCIA (háblale siempre de "tú"): adulto de Latinoamérica, sobre todo Colombia, de 25 a 45 años, con sueldo que \
no alcanza o ingresos inestables. Tiene 1 a 2 horas al día, solo su celular y poco dinero. Ya perdió plata en \
cursos y teme a los gurús de lujos falsos. No quiere ser millonario: quiere un ingreso digital propio, pequeño y \
constante, con un método paso a paso.

REGLAS DE TONO:
- Cercano, directo, honesto. El protagonista es el espectador y su resultado, no tú.
- PROHIBIDO prometer riqueza rápida, ingresos garantizados o "dinero fácil", y mencionar lujos (carros, viajes).
- No inventes cifras. Si usas un dato, elige uno de estos: 48 % de los ocupados en Colombia gana menos de un \
salario mínimo (DANE); 47 % no emprende por miedo a fracasar (GEM 2024); solo 33,5 % de las empresas colombianas \
llega a cinco años (Confecámaras).
- Frases cortas para leer en voz alta. Montos en palabras ("97 dólares").
"""

_PALABRAS_CLAVE_PROMPT = 10       # cuántas búsquedas reales se le ofrecen al modelo


def _bloque_franquicia(tema: str) -> str:
    if not FRANQUICIA_RE.search(tema):
        return ""
    return """
OBLIGATORIO porque el tema es la Franquicia IA. En una o dos frases del desarrollo di, con estas cifras exactas:
cuesta 97 dólares; enseña embudos, pauta y automatización con IA; se gana el 50 %, unos 48,50 dólares, por cada \
venta con el propio enlace; sin ventas no hay ingresos; y tú (el creador) ganas una comisión si entran con tu enlace.
"""


_EJEMPLO_LLM = """{"frase_clave": "ingresos extra desde casa",
 "titulo": "Ingresos extra desde casa 2026: tu primer paso con 1 hora al día",
 "descripcion": "Ingresos extra desde casa sin humo: un paso que haces hoy.\\nSi buscas ingresos extra desde casa \
con poco tiempo, empieza aquí.",
 "hashtags": ["ingresosextra", "emprendimiento", "emprendedores", "trabajardesdecasa", "emprendersinmiedo"],
 "frases": [
  {"texto": "Si tienes una hora al día y solo tu celular, haz esto primero.", "busqueda_en": "person smartphone night"},
  {"texto": "...desarrollo: un obstáculo real y un paso concreto...", "busqueda_en": "notebook planning desk"},
  {"texto": "Si quieres el mapa paso a paso con IA, sin humo ni promesas falsas, revisa el enlace de mi perfil.",
   "busqueda_en": "person smiling phone"}]}"""


def _prompt_llm(tema: str, problemas: list[str] | None = None) -> str:
    claves = ", ".join(f"«{c}»" for c in PALABRAS_CLAVE_SEO[:_PALABRAS_CLAVE_PROMPT])
    correccion = ""
    if problemas:
        correccion = "\nCORRIGE ESTO DE TU VERSIÓN ANTERIOR:\n" + "\n".join(f"- {p}" for p in problemas) + "\n"
    return f"""TEMA: «{tema}». Responde exactamente lo que el tema promete.

GUION: {PALABRAS_OBJETIVO[0]} a {PALABRAS_OBJETIVO[1]} palabras en total (45 a 55 segundos), 6 a 8 frases.
1. Gancho (0-3 s), 6 a 14 palabras, con uno de estos patrones: "Solo con tu celular…", "Si tienes 1 hora al día…", \
"Si yo tuviera que empezar hoy…", "La dura verdad sobre…" o herramienta + resultado realista.
2. Desarrollo: UN obstáculo real (poco tiempo, tutoriales sin orden, miedo a perder dinero o a la estafa) y UN paso \
que pueda hacer hoy, una comparación clara o una herramienta gratuita concreta.
3. Cierre (últimos 8 s), 15 a 22 palabras, sin presión: variante natural de "{CTA_POR_DEFECTO}" Debe mencionar el \
enlace de mi perfil.
{_bloque_franquicia(tema)}
SEO:
- frase_clave: copia tal cual la búsqueda que mejor encaje: {claves}.
- titulo: máximo 70 caracteres, empieza con la frase_clave; puedes añadir "2026" o "Colombia".
- descripcion: 2 líneas; la frase_clave va en la línea 1 y se repite en la línea 2.
- hashtags: 5 a 8, minúsculas, sin "#".
- busqueda_en: 2 a 4 palabras en inglés de una toma de stock filmable, sin lujos.
- Sin emojis, hashtags ni etiquetas tipo "Narrador:" dentro de 'texto'. No añadas "#Shorts" ni nota de afiliados.
{correccion}
EJEMPLO DEL FORMATO (no copies su contenido, solo la forma):
{_EJEMPLO_LLM}"""


ESQUEMA_LLM = {
    "type": "object",
    "properties": {
        "frase_clave": {"type": "string"},
        "titulo": {"type": "string"},
        "descripcion": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "frases": {"type": "array", "items": {
            "type": "object",
            "properties": {"texto": {"type": "string"}, "busqueda_en": {"type": "string"}},
            "required": ["texto", "busqueda_en"], "additionalProperties": False}},
    },
    "required": ["frase_clave", "titulo", "descripcion", "hashtags", "frases"],
    "additionalProperties": False,
}


def _menciona_transparencia(texto: str) -> bool:
    t = sin_acentos(texto.lower())
    return "97" in t and "48" in t and "comision" in t and ("sin ventas" in t or "no hay ingreso" in t)


def problemas_del_guion(guion: Guion) -> list[str]:
    """Reglas del canal que el guion no cumple (se le devuelven al LLM para que corrija)."""
    problemas = []
    n = contar_palabras(guion.texto)
    if not PALABRAS_OBJETIVO[0] <= n <= PALABRAS_OBJETIVO[1]:
        problemas.append(f"El guion tiene {n} palabras; debe tener entre {PALABRAS_OBJETIVO[0]} y "
                         f"{PALABRAS_OBJETIVO[1]} (45 a 55 segundos).")
    if PROMESAS_PROHIBIDAS.search(guion.texto + " " + guion.titulo):
        problemas.append("Promete riqueza, ingresos garantizados o menciona lujos: está prohibido.")
    cierre = sin_acentos(guion.frases[-1].texto.lower()) if guion.frases else ""
    if "enlace" not in cierre and "perfil" not in cierre:
        problemas.append("La última frase debe invitar, sin presión, a revisar el enlace del perfil.")
    if FRANQUICIA_RE.search(guion.tema) and not _menciona_transparencia(guion.texto):
        problemas.append("Falta la transparencia de la Franquicia IA: 97 dólares, 50 % de comisión (48,50 dólares) "
                         "por venta, sin ventas no hay ingresos y que tú ganas comisión con tu enlace.")
    if guion.frase_clave and not guion.titulo.lower().startswith(guion.frase_clave.lower()):
        problemas.append(f"El título debe empezar con la frase clave «{guion.frase_clave}».")
    return problemas


def _guion_desde_json(tema: str, data: dict) -> Guion | None:
    frases = []
    for f in data.get("frases") or []:
        if isinstance(f, dict):
            texto = clean_narration(str(f.get("texto") or ""))
            if contar_palabras(texto) >= 2:
                frases.append(Frase(texto, str(f.get("busqueda_en") or "").strip()))
    if len(frases) < 3:
        return None
    hashtags = [re.sub(r"[^\wáéíóúüñ]", "", str(h).lower().lstrip("#")) for h in data.get("hashtags") or []]
    return Guion(tema=tema, titulo=str(data.get("titulo") or tema).strip(),
                 descripcion=str(data.get("descripcion") or "").strip(),
                 hashtags=[h for h in hashtags if h][:8], frases=frases, fuente="llm",
                 frase_clave=str(data.get("frase_clave") or "").strip())


def guion_con_llm(tema: str) -> Guion | None:
    """Usa el LLM configurado (p. ej. el nivel gratuito de Gemini o Groq) si hay alguna clave.

    Si el guion incumple alguna regla del canal, se le pide una corrección una vez; lo que siga faltando lo
    arregla ``ajustar_a_reglas`` (CTA, transparencia, título)."""
    import time
    try:
        from .config import Settings
        from .llm import LLM, LLMError, available_providers
        settings = Settings.from_env()
        if not available_providers(settings) and not settings.llm_base_url and settings.llm_provider == "auto":
            return None
        llm = LLM(settings)
    except Exception as e:
        log.warning("No se pudo preparar el LLM (%s); uso la plantilla del canal.", e)
        return None
    guion, problemas = None, None
    for _ronda in range(2):
        data = None
        for intento in range(3):                     # Gemini responde 503 cuando está saturado
            try:
                data = llm.complete_json(SISTEMA_LLM, _prompt_llm(tema, problemas), schema=ESQUEMA_LLM,
                                         max_tokens=3000, temperature=0.8)
                break
            except LLMError as e:
                log.warning("El LLM no respondió (intento %d/3): %s", intento + 1, str(e)[:200])
                if intento < 2:
                    time.sleep(20)
        if data is None:
            break
        nuevo = _guion_desde_json(tema, data)
        if nuevo:
            guion = nuevo
            problemas = problemas_del_guion(guion)
            if not problemas:
                break
            log.info("El guion incumple %d regla(s); pido una corrección: %s", len(problemas), " | ".join(problemas))
    if guion is None:
        log.warning("El LLM no dio un guion válido; uso la plantilla del canal.")
    return guion


def elegir_frase_clave(tema: str) -> str:
    t = sin_acentos(tema.lower())
    if re.search(r"\b(ia|inteligencia artificial|chatgpt|gemini|claude)\b", t):
        return "ganar dinero con IA"
    if "sin dinero" in t or "sin invertir" in t:
        return "emprender sin dinero"
    if "instagram" in t:
        return "como vender en instagram desde cero"
    if "producto" in t:
        return "productos digitales como empezar"
    if "cliente" in t:
        return "como conseguir clientes para mi negocio"
    return "ingresos extra desde casa"


def ajustar_a_reglas(guion: Guion) -> Guion:
    """Garantiza lo que no puede quedar a criterio del LLM: cierre con CTA, transparencia comercial y SEO."""
    if not guion.frase_clave:
        guion.frase_clave = elegir_frase_clave(guion.tema)
    cierre = sin_acentos(guion.frases[-1].texto.lower())
    if "enlace" not in cierre and "perfil" not in cierre:
        guion.frases.append(Frase(CTA_POR_DEFECTO))
    if FRANQUICIA_RE.search(guion.tema):
        if not _menciona_transparencia(guion.texto):
            guion.frases.insert(len(guion.frases) - 1, Frase(FRASE_TRANSPARENCIA_FRANQUICIA))
        for f in guion.frases:
            t = sin_acentos(f.texto.lower())
            if "97" in t or "comision" in t or "sin ventas" in t:
                f.fija = True
    guion.frases[0].fija = True
    guion.frases[-1].fija = True
    if guion.fuente == "plantilla":            # la plantilla es genérica: se recorta antes de acelerar la voz
        while contar_palabras(guion.texto) > PALABRAS_OBJETIVO[1]:
            prescindibles = [i for i in range(1, len(guion.frases) - 1) if not guion.frases[i].fija]
            if not prescindibles:
                break
            guion.frases.pop(prescindibles[-1])
    titulo = re.sub(r"\s*#\w+", "", guion.titulo).strip()
    if guion.frase_clave.lower() not in titulo.lower():
        clave = guion.frase_clave[:1].upper() + guion.frase_clave[1:]
        titulo = f"{clave}: {titulo}"
    guion.titulo = titulo
    vistos = []
    for h in [*guion.hashtags, *HASHTAGS_BASE]:
        if h and h not in vistos:
            vistos.append(h)
    guion.hashtags = vistos[:8]
    return guion


ARTICULOS_ES = ("el", "la", "los", "las", "un", "una", "unos", "unas", "lo")


def _titulos_candidatos(tema: str) -> list[str]:
    """«los pulpos» -> ["Los pulpos", "Pulpos", "Pulpo"]: Wikipedia titula sus artículos en singular."""
    palabras = tema.strip().split()
    while len(palabras) > 1 and palabras[0].lower() in ARTICULOS_ES:
        palabras = palabras[1:]
    nucleo = " ".join(palabras)
    candidatos = [tema.strip(), nucleo]
    ultima = palabras[-1] if palabras else ""
    if ultima.lower().endswith("s") and len(ultima) > 3:
        candidatos.append(" ".join(palabras[:-1] + [ultima[:-1]]))
    return [c[:1].upper() + c[1:] for c in dict.fromkeys(candidatos) if c]


def _paginas_wikipedia(params: dict) -> list[dict]:
    r = http().get("https://es.wikipedia.org/w/api.php", timeout=20, params={
        "action": "query", "prop": "extracts|pageprops", "ppprop": "disambiguation", "explaintext": 1,
        "exchars": 3500, "redirects": 1, "format": "json", "formatversion": 2, **params})
    r.raise_for_status()
    paginas = sorted(r.json().get("query", {}).get("pages", []), key=lambda p: p.get("index", 0))
    return [p for p in paginas if not p.get("missing") and "disambiguation" not in (p.get("pageprops") or {})
            and len(p.get("extract") or "") > 200]


def _wikipedia_extracto(tema: str) -> tuple[str, str] | None:
    """Primero busca un artículo con el título exacto (o en singular); si no existe, usa el buscador."""
    candidatos = _titulos_candidatos(tema)
    # Del más específico al más literal: «Pulpo» antes que «Los pulpos» (que podría ser un grupo musical).
    for titulo in reversed(candidatos):
        for pagina in _paginas_wikipedia({"titles": titulo}):
            return pagina["title"], pagina["extract"]
    nucleo = candidatos[1] if len(candidatos) > 1 else candidatos[0]
    for pagina in _paginas_wikipedia({"generator": "search", "gsrsearch": nucleo, "gsrlimit": 3}):
        return pagina["title"], pagina["extract"]
    return None


def _limpiar_wikipedia(texto: str) -> str:
    texto = re.split(r"\n=+\s*(?:Referencias|Véase también|Notas|Bibliografía|Enlaces externos)", texto)[0]
    texto = re.sub(r"^=+.*?=+\s*$", " ", texto, flags=re.M)     # títulos de sección
    texto = re.sub(r"\([^()]*\)", "", texto)          # pronunciaciones, fechas entre paréntesis
    texto = re.sub(r"\[[^\]]*\]", "", texto)
    texto = re.sub(r"\s+([,.;:])", r"\1", texto)
    return re.sub(r"\s+", " ", texto).strip()


def guion_con_wikipedia(tema: str) -> Guion | None:
    try:
        res = with_retries(lambda: _wikipedia_extracto(tema), attempts=2, label="Wikipedia")
    except Exception as e:
        log.warning("Wikipedia no respondió (%s).", e)
        return None
    if not res:
        return None
    titulo_wiki, extracto = res
    cuerpo, total = [], 0
    for frase in dividir_frases(_limpiar_wikipedia(extracto)):
        n = contar_palabras(frase)
        if n > 40:                                    # frases enciclopédicas demasiado largas
            frase = truncate_words(frase, 220).rstrip(",;:") + "."
            n = contar_palabras(frase)
        if total + n > PALABRAS_OBJETIVO[1] - 25:
            break
        cuerpo.append(frase)
        total += n
    if not cuerpo:
        return None
    gancho = random.choice([
        f"¿Qué tanto sabes realmente sobre {tema}?",
        f"Esto es lo que casi nadie te cuenta sobre {tema}.",
        f"Te sorprenderá lo que hay detrás de {tema}.",
    ])
    cierre = "Si aprendiste algo nuevo, sígueme para más datos curiosos."
    frases = [Frase(gancho)] + [Frase(f) for f in cuerpo] + [Frase(cierre)]
    tema_tag = re.sub(r"[^\wáéíóúüñ]", "", sin_acentos(tema.lower()).replace(" ", ""))
    return Guion(
        tema=tema,
        titulo=truncate_words(f"{titulo_wiki}: lo que no sabías", 70),
        descripcion=(f"Datos rápidos sobre {titulo_wiki} en menos de un minuto. "
                     f"Fuente: Wikipedia en español (CC BY-SA 4.0)."),
        hashtags=[t for t in [tema_tag, "datoscuriosos", "sabiasque", "aprendeentiktok", "curiosidades",
                              "shortsenespañol"] if t],
        frases=frases, fuente="wikipedia")


def guion_plantilla(tema: str) -> Guion:
    """Sin LLM: un guion antihumo genérico para el cliente ideal, siempre en español."""
    frases = [
        (f"Si tienes solo una hora al día y tu celular, escucha esto sobre {tema}.", "person using smartphone night"),
        ("El problema casi nunca es la falta de talento, es saltar de tutorial en tutorial sin orden.",
         "tired person laptop"),
        ("Por eso hoy no vas a comprar nada: vas a escoger un solo camino y un solo paso.", "notebook planning desk"),
        ("Abre las notas de tu celular y escribe qué sabes hacer y quién te ha pagado por eso.",
         "writing notes smartphone"),
        ("Con eso defines una oferta pequeña que puedes probar esta semana con herramientas gratis.",
         "small business online"),
        ("Tu meta no es un millón, es lograr tu primer resultado pequeño y medible.", "checklist goal"),
        ("Y desconfía de quien te muestre carros y viajes en lugar de un proceso claro.", "thinking person doubt"),
        (CTA_POR_DEFECTO, "person smiling smartphone"),
    ]
    clave = elegir_frase_clave(tema)
    clave_titulo = clave[:1].upper() + clave[1:]
    return Guion(
        tema=tema,
        titulo=recortar_titulo(f"{clave_titulo} 2026: paso a paso y sin humo" if clave in tema.lower()
                               else f"{clave_titulo} 2026: {re.split(r'[:,.;¿?]', tema)[0].strip()}, sin humo", 70),
        descripcion=(f"{clave_titulo}: un paso concreto sobre {tema}, sin humo y solo con tu celular.\n"
                     f"Si buscas {clave} con 1 hora al día, empieza por aquí."),
        hashtags=["ingresosextra", "emprendimiento", "emprendedores", "emprendersindinero"],
        frases=[Frase(t, q) for t, q in frases], fuente="plantilla", frase_clave=clave)


def generar_guion(tema: str, usar_llm: bool = True, usar_wikipedia: bool = False) -> Guion:
    """LLM (con las reglas del canal) -> Wikipedia (solo si se pide; da guiones enciclopédicos) -> plantilla."""
    guion = ((guion_con_llm(tema) if usar_llm else None)
             or (guion_con_wikipedia(tema) if usar_wikipedia else None)
             or guion_plantilla(tema))
    guion = ajustar_a_reglas(guion)
    log.info("Guion generado (%s): %d frases, %d palabras.", guion.fuente, len(guion.frases),
             contar_palabras(guion.texto))
    tema_en = consulta_tema(tema)
    ultima = len(guion.frases) - 1
    for i, frase in enumerate(guion.frases):
        busqueda = frase.busqueda_en.strip()
        if busqueda and not re.search(r"[áéíóúñ¿¡]", busqueda, re.I):
            continue                                    # el LLM ya dio una buena consulta en inglés
        frase.busqueda_en = tema_en
        if 0 < i < ultima:                              # el gancho y el cierre muestran el tema
            clave = next((p for p in palabras_clave(frase.texto, 3)
                          if sin_acentos(p) not in sin_acentos(tema.lower())), "")
            extra = traducir_al_ingles(clave) if clave else ""
            extra = " ".join(w for w in extra.split() if w not in STOPWORDS_EN and w not in tema_en.split())
            if extra:
                frase.busqueda_en = f"{tema_en} {singular_en(extra.split()[0])}"
    return guion


# ------------------------------------------------------------------------------------------ voz
@dataclass
class Palabra:
    texto: str
    inicio: float
    fin: float


@dataclass
class Locucion:
    audio: Path
    duracion: float
    palabras: list[Palabra]
    tramos: list[tuple[float, float]] = field(default_factory=list)   # (inicio, fin) de cada frase


async def _edge_frase(texto: str, voz: str, velocidad: str, destino: Path) -> list[Palabra]:
    import edge_tts
    comm = edge_tts.Communicate(texto, voz, rate=velocidad, boundary="WordBoundary")
    palabras: list[Palabra] = []
    with open(destino, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                ini = chunk["offset"] / 1e7
                palabras.append(Palabra(chunk["text"], ini, ini + chunk["duration"] / 1e7))
    if destino.stat().st_size == 0:
        raise RuntimeError("edge-tts no devolvió audio")
    return palabras


def sintetizar(frases: list[str], carpeta: Path, voz: str, velocidad: str) -> Locucion:
    """Sintetiza cada frase por separado (así se conoce exactamente dónde empieza cada una)."""
    wavs, palabras, tramos, t = [], [], [], 0.0
    pausa = 0.12
    for i, texto in enumerate(frases):
        mp3 = carpeta / f"frase_{i:02d}.mp3"
        wav = carpeta / f"frase_{i:02d}.wav"
        pals = with_retries(lambda texto=texto, mp3=mp3: asyncio.run(_edge_frase(texto, voz, velocidad, mp3)),
                            attempts=3, label=f"edge-tts frase {i + 1}")
        ffmpeg.run(["-i", str(mp3), "-af", f"apad=pad_dur={pausa}", "-ar", "48000", "-ac", "1", str(wav)],
                   label="voz a wav")
        dur = ffmpeg.duration(wav)
        palabras += [Palabra(p.texto, p.inicio + t, p.fin + t) for p in pals]
        tramos.append((t, t + dur))
        wavs.append(wav)
        t += dur
    lista = carpeta / "voz.txt"
    lista.write_text("".join(f"file '{w.name}'\n" for w in wavs), encoding="utf-8")
    audio = carpeta / "voz.wav"
    ffmpeg.run(["-f", "concat", "-safe", "0", "-i", lista.name, "-c", "copy", audio.name], cwd=carpeta,
               label="unir voz")
    return Locucion(audio, ffmpeg.duration(audio), palabras, tramos)


def _velocidad_pct(velocidad: str) -> int:
    m = re.match(r"^\s*([+-]?\d+)\s*%\s*$", velocidad)
    return int(m.group(1)) if m else 0


def locucion_bajo_60s(guion: Guion, carpeta: Path, voz: str, velocidad: str) -> Locucion:
    """Sintetiza y, si pasa de DURACION_MAXIMA, acelera la voz o recorta frases del cuerpo."""
    frases = list(guion.frases)
    pct = _velocidad_pct(velocidad)
    for _ in range(8):
        loc = sintetizar([f.texto for f in frases], carpeta, voz, f"{pct:+d}%")
        if loc.duracion <= DURACION_MAXIMA:
            guion.frases = frases
            return loc
        exceso = loc.duracion / DURACION_MAXIMA - 1
        if pct < VELOCIDAD_MAXIMA and exceso < 0.25:
            pct = min(VELOCIDAD_MAXIMA, pct + math.ceil(exceso * 100) + 3)
            log.info("La locución dura %.1f s; subo la velocidad a %+d%%.", loc.duracion, pct)
        elif any(not f.fija for f in frases[1:-1]):
            # Se quita la última frase del desarrollo que no sea obligatoria (gancho, transparencia y cierre
            # se conservan siempre).
            idx = max(i for i in range(1, len(frases) - 1) if not frases[i].fija)
            quitada = frases.pop(idx)
            log.info("La locución dura %.1f s; quito la frase: %s", loc.duracion, quitada.texto)
        else:
            break
    raise RuntimeError(f"No se pudo dejar la locución por debajo de {DURACION_MAXIMA:.0f} s")


# ------------------------------------------------------------------------------------------ Pixabay
@dataclass
class Clip:
    id: str
    url: str
    ancho: int
    alto: int
    duracion: float
    autor: str

    @property
    def vertical(self) -> bool:
        return self.alto > self.ancho


def elegir_archivo_pixabay(videos: dict) -> dict | None:
    """El archivo HD (lado corto >= 720 px) más ligero; si no hay HD, el más grande disponible."""
    opciones = [v for v in (videos or {}).values() if isinstance(v, dict) and v.get("url")]
    hd = [v for v in opciones if min(v.get("width", 0), v.get("height", 0)) >= 720]
    if hd:
        return min(hd, key=lambda v: v.get("width", 0) * v.get("height", 0))
    return max(opciones, key=lambda v: v.get("width", 0) * v.get("height", 0), default=None)


def buscar_pixabay(consulta: str, clave: str) -> list[Clip]:
    """Busca videos (consulta en inglés). Los verticales HD van primero; los horizontales se recortan a 9:16."""
    r = http().get("https://pixabay.com/api/videos/", timeout=30, params={
        "key": clave, "q": consulta[:100], "lang": "en", "per_page": 50, "safesearch": "true",
        "min_height": 720, "order": "popular"})
    if r.status_code in (400, 401, 403):
        raise RuntimeError(f"Pixabay rechazó la petición (HTTP {r.status_code}). Revisa PIXABAY_API_KEY.")
    r.raise_for_status()
    clips = []
    for h in r.json().get("hits", []):
        f = elegir_archivo_pixabay(h.get("videos") or {})
        if f:
            clips.append(Clip(str(h["id"]), f["url"], int(f.get("width", 0)), int(f.get("height", 0)),
                              float(h.get("duration") or 0), str(h.get("user") or "")))
    clips.sort(key=lambda c: (not c.vertical, min(c.ancho, c.alto) < 1080))   # estable: respeta popularidad
    return clips


PAUSA_ENTRE_DESCARGAS = 3.0      # segundos: el CDN de Pixabay responde 429 a las ráfagas de descargas
MAX_FALLOS_SEGUIDOS = 3          # después se reutilizan los clips ya descargados


def descargar_clip(url: str, destino: Path) -> Path:
    """Descarga un clip; ante un 429 espera lo que pida el servidor (Retry-After) o 15-45 s."""
    import time
    destino.parent.mkdir(parents=True, exist_ok=True)
    for intento in range(3):
        with http().get(url, stream=True, timeout=120) as r:
            if r.status_code == 429:
                cabecera = r.headers.get("Retry-After", "")
                espera = min(90.0, float(cabecera)) if cabecera.replace(".", "", 1).isdigit() \
                    else 15.0 * (intento + 1)
                log.info("Pixabay pide esperar (429); reintento en %.0f s…", espera)
                time.sleep(espera)
                continue
            r.raise_for_status()
            tmp = destino.with_suffix(".part")
            tamano = 0
            with open(tmp, "wb") as f:
                for trozo in r.iter_content(1 << 16):
                    tamano += len(trozo)
                    if tamano > 150 * 1024 * 1024:
                        raise RuntimeError("clip demasiado grande")
                    f.write(trozo)
            if tamano == 0:
                raise RuntimeError("descarga vacía")
            tmp.replace(destino)
            return destino
    raise RuntimeError("HTTP 429: límite de descargas de Pixabay")


class BuscadorClips:
    def __init__(self, clave: str, carpeta: Path, consulta_general: str):
        self.clave = clave
        self.carpeta = carpeta
        self.general = consulta_general
        self.usados: set[str] = set()
        self.autores: set[str] = set()
        self.descargados: list[Path] = []
        self.fallos_seguidos = 0
        self._cache: dict[str, list[Clip]] = {}

    def _resultados(self, consulta: str) -> list[Clip]:
        if consulta not in self._cache:
            try:
                self._cache[consulta] = with_retries(lambda: buscar_pixabay(consulta, self.clave), attempts=2,
                                                     label=f"Pixabay '{consulta}'")
                if not self._cache[consulta]:
                    log.info("Pixabay no tiene clips para '%s'.", consulta)
            except Exception as e:
                log.warning("Búsqueda en Pixabay fallida para '%s': %s", consulta, e)
                self._cache[consulta] = []
        return self._cache[consulta]

    def _reutilizar(self, n: int) -> Path | None:
        if not self.descargados:
            return None
        clip = self.descargados[n % len(self.descargados)]
        log.info("Toma %d: reutilizo %s", n, clip.name)
        return clip

    def clip_para(self, consulta: str, n: int) -> Path | None:
        """Descarga un clip no usado para la consulta (o versiones más generales de ella)."""
        import time
        if self.fallos_seguidos >= MAX_FALLOS_SEGUIDOS:
            return self._reutilizar(n)
        sing = singular_en(consulta).split()
        intentos = [consulta, " ".join(sing), " ".join(sing[:2]), sing[0] if sing else "", self.general]
        for q in dict.fromkeys(i for i in intentos if i):
            for clip in self._resultados(q):
                if clip.id in self.usados:
                    continue
                self.usados.add(clip.id)
                destino = self.carpeta / f"pixabay_{clip.id}.mp4"
                try:
                    if self.descargados:
                        time.sleep(PAUSA_ENTRE_DESCARGAS)
                    descargar_clip(clip.url, destino)
                except Exception as e:
                    self.fallos_seguidos += 1
                    log.warning("No se pudo descargar el clip %s: %s", clip.id, e)
                    if self.fallos_seguidos >= MAX_FALLOS_SEGUIDOS:
                        log.warning("Demasiadas descargas fallidas seguidas; reutilizo los clips ya descargados.")
                        return self._reutilizar(n)
                    continue
                self.fallos_seguidos = 0
                self.descargados.append(destino)
                if clip.autor:
                    self.autores.add(clip.autor)
                log.info("Toma %d: '%s' -> Pixabay %s (%dx%d%s)", n, q, clip.id, clip.ancho, clip.alto,
                         ", vertical" if clip.vertical else ", recortado a 9:16")
                return destino
        return self._reutilizar(n)


# ------------------------------------------------------------------------------------------ subtítulos
def _tiempo_ass(t: float) -> str:
    cs = max(0, round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _tiempo_srt(t: float) -> str:
    ms = max(0, round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def agrupar_subtitulos(palabras: list[Palabra], maximo: int = PALABRAS_POR_SUBTITULO,
                       duracion_total: float | None = None) -> list[tuple[float, float, str]]:
    """Bloques de pocas palabras; cada bloque dura hasta que empieza el siguiente (sin parpadeos)."""
    bloques: list[list[Palabra]] = []
    actual: list[Palabra] = []
    for p in palabras:
        if actual and p.inicio - actual[-1].fin > 0.25:     # pausa natural (fin de frase): bloque nuevo
            bloques.append(actual)
            actual = []
        actual.append(p)
        if len(actual) >= maximo or re.search(r"[.!?…,;:]$", p.texto):
            bloques.append(actual)
            actual = []
    if actual:
        bloques.append(actual)
    salida = []
    for i, b in enumerate(bloques):
        inicio = b[0].inicio
        fin = b[-1].fin + 0.25
        if i + 1 < len(bloques):
            fin = min(max(fin, b[-1].fin), bloques[i + 1][0].inicio)
        elif duracion_total:
            fin = min(fin, duracion_total)
        texto = " ".join(p.texto for p in b)
        salida.append((inicio, max(fin, inicio + 0.1), texto))
    return salida


def _escapar_ass(texto: str) -> str:
    return texto.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def escribir_ass(bloques: list[tuple[float, float, str]], destino: Path, fuente: str = "DejaVu Sans") -> Path:
    cabecera = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {ANCHO}
PlayResY: {ALTO}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, \
Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, \
MarginV, Encoding
Style: Sub,{fuente},92,&H00FFFFFF,&H0000E5FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,7,3,2,90,90,640,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lineas = []
    for inicio, fin, texto in bloques:
        # Pequeño "pop" de escala al aparecer cada bloque, típico de los Shorts.
        efecto = r"{\fscx85\fscy85\t(0,90,\fscx100\fscy100)}"
        lineas.append(f"Dialogue: 0,{_tiempo_ass(inicio)},{_tiempo_ass(fin)},Sub,,0,0,0,,"
                      f"{efecto}{_escapar_ass(texto.upper())}")
    destino.write_text(cabecera + "\n".join(lineas) + "\n", encoding="utf-8")
    return destino


def escribir_srt(bloques: list[tuple[float, float, str]], destino: Path) -> Path:
    partes = [f"{i}\n{_tiempo_srt(a)} --> {_tiempo_srt(b)}\n{t}\n" for i, (a, b, t) in enumerate(bloques, 1)]
    destino.write_text("\n".join(partes), encoding="utf-8")
    return destino


# ------------------------------------------------------------------------------------------ render
_FILTRO_VERTICAL = (f"scale={ANCHO}:{ALTO}:force_original_aspect_ratio=increase,crop={ANCHO}:{ALTO},"
                    f"setsar=1,fps={FPS},format=yuv420p")


def preparar_toma(origen: Path | None, duracion: float, destino: Path, color: str = "0x1b1f3a") -> Path:
    """Normaliza un clip a 1080x1920, 30 fps, sin audio y con la duración exacta (en bucle si es corto)."""
    comunes = ["-t", f"{duracion:.3f}", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-pix_fmt", "yuv420p", str(destino)]
    if origen is None:
        ffmpeg.run(["-f", "lavfi", "-i", f"color=c={color}:s={ANCHO}x{ALTO}:r={FPS}", "-vf", "format=yuv420p",
                    *comunes], label="toma de color")
    else:
        ffmpeg.run(["-stream_loop", "-1", "-i", str(origen), "-vf", _FILTRO_VERTICAL, *comunes], label="toma")
    return destino


def planificar_tomas(tramos: list[tuple[float, float]], duracion_total: float) -> list[tuple[int, float]]:
    """(índice de frase, duración) de cada toma; las frases largas se reparten en varias tomas."""
    tomas = []
    for i, (ini, fin) in enumerate(tramos):
        if i == len(tramos) - 1:
            fin = duracion_total
        largo = max(0.2, fin - ini)
        partes = max(1, math.ceil(largo / MAX_SEGUNDOS_POR_TOMA))
        tomas += [(i, largo / partes)] * partes
    return tomas


def renderizar(guion: Guion, loc: Locucion, carpeta: Path, salida: Path, clave_pixabay: str) -> set[str]:
    duracion = loc.duracion + 0.35
    tomas = planificar_tomas(loc.tramos, duracion)
    buscador = BuscadorClips(clave_pixabay, carpeta / "clips", consulta_tema(guion.tema)) \
        if clave_pixabay else None
    if not buscador:
        log.warning("PIXABAY_API_KEY no está definida: el fondo será un color liso.")
    clips = [buscador.clip_para(guion.frases[i].busqueda_en, n + 1) if buscador else None
             for n, (i, _) in enumerate(tomas)]
    if buscador and buscador.descargados:              # las tomas sin clip propio reutilizan uno descargado
        clips = [c or buscador._reutilizar(n + 1) for n, c in enumerate(clips)]
    elif buscador:
        log.warning("No se encontró ningún clip en Pixabay: el fondo será un color liso.")
    colores = ["0x1b1f3a", "0x2a1b3a", "0x13303a", "0x3a1b1f"]
    partes = [preparar_toma(clip, dur, carpeta / f"toma_{n:02d}.mp4", colores[i % len(colores)])
              for n, (clip, (i, dur)) in enumerate(zip(clips, tomas))]
    lista = carpeta / "tomas.txt"
    lista.write_text("".join(f"file '{p.name}'\n" for p in partes), encoding="utf-8")
    ffmpeg.run(["-f", "concat", "-safe", "0", "-i", lista.name, "-c", "copy", "fondo.mp4"], cwd=carpeta,
               label="unir tomas")

    bloques = agrupar_subtitulos(loc.palabras, duracion_total=duracion)
    escribir_ass(bloques, carpeta / "subtitulos.ass")
    escribir_srt(bloques, salida.with_name("subtitulos.srt"))

    # Voz con volumen normalizado (-14 LUFS, el estándar de YouTube) y subtítulos quemados en el video.
    ffmpeg.run(["-i", "fondo.mp4", "-i", loc.audio.name,
                "-filter_complex",
                "[0:v]ass=subtitulos.ass[v];[1:a]apad,loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]",
                "-map", "[v]", "-map", "[a]", "-t", f"{duracion:.3f}",
                "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
                "-profile:v", "high", "-r", str(FPS),
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(salida.resolve())],
               cwd=carpeta, label="render final", timeout=1800)
    return buscador.autores if buscador else set()


# ------------------------------------------------------------------------------------------ todo junto
def descripcion_final(guion: Guion, autores: set[str]) -> str:
    """Frase clave en las dos primeras líneas, nota de afiliados, créditos y hashtags."""
    lineas = [ln.strip() for ln in guion.descripcion.strip().splitlines() if ln.strip()]
    clave = guion.frase_clave
    if clave:
        clave_m = clave[:1].upper() + clave[1:]
        if not lineas or clave.lower() not in lineas[0].lower():
            lineas.insert(0, f"{clave_m}: {guion.tema}, explicado sin humo y paso a paso.")
        if len(lineas) < 2 or clave.lower() not in lineas[1].lower():
            lineas.insert(1, f"Si buscas {clave} con poco tiempo y solo tu celular, este video es para ti.")
    partes = ["\n".join(lineas), NOTA_TRANSPARENCIA]
    if autores:
        partes.append("Videos de fondo: Pixabay (" + ", ".join(sorted(autores)) + ").")
    partes.append("Voz generada con inteligencia artificial.")
    partes.append(" ".join(f"#{h}" for h in [*guion.hashtags, "shorts"]))
    return "\n\n".join(p for p in partes if p)


_CONECTORES_FINALES = {"y", "o", "de", "del", "con", "para", "que", "la", "el", "los", "las", "en", "a", "sobre",
                       "por", "un", "una", "qué", "cuánto", "cómo", "quién"}


def recortar_titulo(titulo: str, limite: int) -> str:
    """Recorta en una palabra completa y sin dejar un conector colgando ("…cuánto cuesta y")."""
    if len(titulo) <= limite:
        return titulo
    palabras = truncate_words(titulo, limite).split()
    while len(palabras) > 1 and (palabras[-1].lower().strip(",:;") in _CONECTORES_FINALES):
        palabras.pop()
    return " ".join(palabras).rstrip(" ,:;-")


def titulo_final(guion: Guion) -> str:
    titulo = re.sub(r"\s*#\w+", "", guion.titulo).strip()
    return recortar_titulo(titulo, 90) + " #Shorts"


def crear_short(tema: str, carpeta_salida: str | Path = "salida", *, voz: str = VOZ_POR_DEFECTO,
                velocidad: str = VELOCIDAD_POR_DEFECTO, clave_pixabay: str = "", usar_llm: bool = True,
                usar_wikipedia: bool = False, conservar_temporales: bool = False) -> Path:
    tema = re.sub(r"\s+", " ", tema).strip()
    if not tema:
        raise ValueError("El tema no puede estar vacío.")
    destino = Path(carpeta_salida) / (slugify(sin_acentos(tema)) or "short")
    destino.mkdir(parents=True, exist_ok=True)
    trabajo = destino / "_temporal"
    trabajo.mkdir(exist_ok=True)

    log.info("1/4 Escribiendo el guion sobre «%s»…", tema)
    guion = generar_guion(tema, usar_llm=usar_llm, usar_wikipedia=usar_wikipedia)
    log.info("2/4 Grabando la voz (%s)…", voz)
    loc = locucion_bajo_60s(guion, trabajo, voz, velocidad)
    log.info("    Duración de la locución: %.1f s", loc.duracion)
    log.info("3/4 Buscando clips verticales en Pixabay y montando el video…")
    video = destino / "short.mp4"
    autores = renderizar(guion, loc, trabajo, video, clave_pixabay)
    log.info("4/4 Guardando título, descripción y guion…")

    titulo = titulo_final(guion)
    descripcion = descripcion_final(guion, autores)
    log.info("Título: %s", titulo)
    log.info("Guion (%s):\n%s", guion.fuente, "\n".join(f"  {n}. {f.texto}" for n, f in enumerate(guion.frases, 1)))
    pendientes = problemas_del_guion(guion)
    if pendientes:
        log.warning("Reglas sin cumplir: %s", " | ".join(pendientes))
    (destino / "titulo.txt").write_text(titulo + "\n", encoding="utf-8")
    (destino / "descripcion.txt").write_text(descripcion + "\n", encoding="utf-8")
    (destino / "guion.txt").write_text("\n".join(f.texto for f in guion.frases) + "\n", encoding="utf-8")
    meta = {"tema": tema, "titulo": titulo, "frase_clave": guion.frase_clave, "descripcion": descripcion,
            "hashtags": guion.hashtags, "reglas_pendientes": pendientes,
            "fuente_guion": guion.fuente, "voz": voz, "duracion_segundos": round(ffmpeg.duration(video), 2),
            "frases": [asdict(f) for f in guion.frases]}
    (destino / "metadatos.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if not conservar_temporales:
        shutil.rmtree(trabajo, ignore_errors=True)
    log.info("Listo: %s (%.1f s)", video, meta["duracion_segundos"])
    return video


def construir_parser() -> argparse.ArgumentParser:
    import os
    p = argparse.ArgumentParser(prog="short_es",
                                description="Genera un YouTube Short vertical en español (menos de 60 s).")
    p.add_argument("tema_pos", nargs="?", metavar="TEMA", help="tema del video")
    p.add_argument("--tema", help="tema del video (alternativa al argumento posicional)")
    p.add_argument("--salida", default=os.getenv("CARPETA_SALIDA", "salida"), help="carpeta de salida")
    p.add_argument("--voz", default=os.getenv("VOZ_ES", VOZ_POR_DEFECTO), help="voz de edge-tts")
    p.add_argument("--velocidad", default=os.getenv("VELOCIDAD_VOZ", VELOCIDAD_POR_DEFECTO),
                   help="velocidad de la voz, p. ej. +8%%")
    p.add_argument("--sin-llm", action="store_true", help="no usar LLM aunque haya clave (usa la plantilla)")
    p.add_argument("--wikipedia", action="store_true",
                   help="si el LLM falla, usar Wikipedia en español (guion enciclopédico, fuera del nicho)")
    p.add_argument("--conservar-temporales", action="store_true", help="no borrar la carpeta _temporal")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    import os
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:  # pragma: no cover
        pass
    args = construir_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    tema = args.tema or args.tema_pos
    if not tema:
        print("Indica un tema, por ejemplo:  python -m purffle_shorts.short_es --tema \"los pulpos\"",
              file=sys.stderr)
        return 2
    try:
        video = crear_short(tema, args.salida, voz=args.voz, velocidad=args.velocidad,
                            clave_pixabay=os.getenv("PIXABAY_API_KEY", "").strip(), usar_llm=not args.sin_llm,
                            usar_wikipedia=args.wikipedia,
                            conservar_temporales=args.conservar_temporales)
    except Exception as e:
        log.error("Error al generar el Short: %s", e)
        if args.verbose:
            raise
        return 1
    print(video)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
