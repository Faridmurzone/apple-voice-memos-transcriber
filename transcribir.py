#!/usr/bin/env python3
"""Transcribe y diariza un audio (p. ej. una nota de voz de macOS) con Google STT chirp_3.

Uso:
    python3 transcribir.py listar                 # notas de voz disponibles
    python3 transcribir.py run <audio|n°> [--nombre X]

<n°> es el índice que muestra `listar`. Cada audio va en su propia carpeta
(output/<nombre>/, ignorada por git) con transcripcion.txt y transcripcion.srt.
El proceso es reanudable: los pasos ya hechos (tramos, JSON de salida) no se repiten.

Pasos:
 1. ffmpeg -> FLAC mono 16 kHz (primera pista de audio; las .qta traen pistas extra).
 2. Tramos de 19 min con 60 s de solape (chirp_3: máx. 20 min por archivo, 5 por request).
 3. Bucket privado temporal en GCS, BatchRecognize chirp_3 es-US con diarización.
 4. Une tramos: encadena hablantes votando palabras del minuto compartido.
 5. Detecta huecos (chirp_3 omite minutos sin avisar) y los re-transcribe en clips de 10 min.
 6. Escribe TXT/SRT y borra el bucket.
 7. Genera resumen.md con Claude (resumir.py), salvo --sin-resumen.

Notas: las llamadas HTTP van por curl porque el Python del sistema falla con SSL.
No se loguea contenido de la transcripción, solo conteos.
"""
import argparse
import json
import math
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from config import OUTPUT, proyecto

REGION = "us"
IDIOMA = "es-US"
NOTAS = Path.home() / "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"

PASO, LARGO = 1080, 1140          # tramo de 19 min, solape de 60 s
PARCHE = 600                       # clip de re-transcripción sobre huecos
MAX_DUR_PALABRA = 30               # marcas de tiempo corruptas de chirp_3
API = f"https://{REGION}-speech.googleapis.com/v2"
PUNT = re.compile(r"[^\w]+")


# ---------------------------------------------------------------- utilidades

def sh(*args, capturar=True):
    r = subprocess.run(args, capture_output=capturar, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"falló {args[0]}: {(r.stderr or '')[-500:]}")
    return r.stdout


def token():
    return sh("gcloud", "auth", "print-access-token").strip()


def curl(metodo, url, cuerpo=None):
    args = ["curl", "-sS", "-X", metodo, url,
            "-H", f"Authorization: Bearer {token()}",
            "-H", f"x-goog-user-project: {proyecto()}",
            "-H", "Content-Type: application/json"]
    if cuerpo is not None:
        args += ["-d", json.dumps(cuerpo)]
    resp = json.loads(sh(*args) or "{}")
    if "error" in resp:
        raise RuntimeError(f"API: {resp['error'].get('message')}")
    return resp


def duracion(audio):
    return float(sh("ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "csv=p=0", str(audio)))


def cortar(origen, destino, inicio, largo):
    sh("ffmpeg", "-v", "error", "-y", "-ss", str(inicio), "-t", str(largo),
       "-i", str(origen), "-c:a", "flac", str(destino))


def norm(palabra):
    s = unicodedata.normalize("NFKD", palabra.lower())
    return PUNT.sub("", "".join(c for c in s if not unicodedata.combining(c)))


def hms(seg, srt=False):
    h, r = divmod(seg, 3600)
    m, s = divmod(r, 60)
    if srt:
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int(round((s % 1) * 1000)) % 1000:03d}"
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d}"


# ---------------------------------------------------------------- notas de voz

def notas_de_voz():
    db = NOTAS / "CloudRecordings.db"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.execute("select 1 from ZCLOUDRECORDING limit 1")
    except sqlite3.Error:
        sys.exit("no pude leer la base de Notas de Voz: dale a tu terminal \"Acceso total al disco\" en "
                 "Ajustes del Sistema > Privacidad y seguridad (o usá `run <ruta al audio>`)")
    filas = con.execute(
        "select ZCUSTOMLABEL, datetime(ZDATE+978307200,'unixepoch','localtime'), ZDURATION, ZPATH "
        "from ZCLOUDRECORDING where ZPATH is not null and ZDURATION > 5 order by ZDATE desc").fetchall()
    return [(etq, fecha, dur, NOTAS / ruta) for etq, fecha, dur, ruta in filas if (NOTAS / ruta).exists()]


def listar():
    for i, (etq, fecha, dur, ruta) in enumerate(notas_de_voz()):
        print(f"{i:>3}  {fecha}  {dur / 60:6.1f} min  {etq or ''}  [{ruta.name}]")


# ---------------------------------------------------------------- GCS + STT

def subir(archivos, bucket, prefijo):
    sh("gcloud", "storage", "cp", *map(str, archivos), f"gs://{bucket}/{prefijo}/")
    return [f"gs://{bucket}/{prefijo}/{a.name}" for a in archivos]


def reconocer(uris, bucket, salida):
    """BatchRecognize en lotes de 5 y espera a que terminen todas las operaciones."""
    url = f"{API}/projects/{proyecto()}/locations/{REGION}/recognizers/_:batchRecognize"
    ops = []
    for n in range(0, len(uris), 5):
        cuerpo = {
            "config": {"autoDecodingConfig": {}, "model": "chirp_3", "languageCodes": [IDIOMA],
                       "features": {"enableAutomaticPunctuation": True, "enableWordTimeOffsets": True,
                                    "diarizationConfig": {}}},
            "files": [{"uri": u} for u in uris[n:n + 5]],
            "recognitionOutputConfig": {"gcsOutputConfig": {"uri": f"gs://{bucket}/{salida}/"}},
        }
        ops.append(curl("POST", url, cuerpo)["name"])
    print(f"  {len(ops)} operación(es) lanzada(s); esperando…", flush=True)
    t0 = time.time()
    pendientes = set(ops)
    fallas = []
    while pendientes:
        time.sleep(20)
        for op in list(pendientes):
            r = curl("GET", f"{API}/{op}")
            if r.get("done"):
                pendientes.discard(op)
                errores = [v["error"] for v in r.get("response", {}).get("results", {}).values()
                           if v.get("error", {}).get("code")]
                if r.get("error", {}).get("code") or errores:
                    fallas.append(r.get("error") or errores)
        if time.time() - t0 > 3600:
            raise TimeoutError(f"STT no terminó en 1 h: {pendientes}")
    print(f"  listo en {int(time.time() - t0)} s", flush=True)
    if fallas:   # lo que sí salió queda en el bucket; run() lo baja antes de abortar
        raise RuntimeError(f"STT falló en {len(fallas)} lote(s): {fallas}")


def bajar(bucket, salida, destino):
    destino.mkdir(parents=True, exist_ok=True)
    sh("gcloud", "storage", "cp", f"gs://{bucket}/{salida}/*", str(destino))


def palabras(json_path, offset, largo=LARGO):
    """[(inicio, fin, palabra, hablante)] en tiempo absoluto.

    chirp_3 a veces devuelve finales absurdos con un inicio correcto (se corrige el fin),
    inicios fuera del clip (se descartan) y repite segmentos con otra etiqueta (se deduplica).
    """
    out, vistas = [], set()
    for r in json.load(open(json_path)).get("results", []):
        for w in (r.get("alternatives") or [{}])[0].get("words", []):
            if "startOffset" not in w or w.get("word") in ("[", "]", None):
                continue
            a = float(w["startOffset"].rstrip("s"))
            b = float(w.get("endOffset", w["startOffset"]).rstrip("s"))
            if a > largo + 1:
                continue
            if b < a or b - a > MAX_DUR_PALABRA:
                b = a + 0.5
            clave = (round(a, 2), norm(w["word"]))
            if clave in vistas:
                continue
            vistas.add(clave)
            out.append((a + offset, b + offset, w["word"], w.get("speakerLabel", "?")))
    return sorted(out)


def json_de(directorio, nombre):
    hits = sorted(directorio.glob(f"{nombre}_transcript_*.json"))
    if not hits:
        raise FileNotFoundError(f"sin salida STT para {nombre}")
    return hits[-1]


# ---------------------------------------------------------------- unión y hablantes

def votar(nuevas, ref):
    """Mapea etiquetas de `nuevas` a las de `ref` emparejando palabras iguales a <1,5 s."""
    votos = defaultdict(Counter)
    for a, _, w, h in nuevas:
        n = norm(w)
        if not n:
            continue
        for ra, _, rw, rh in ref:
            if abs(ra - a) < 1.5 and norm(rw) == n:
                votos[h][rh] += 1
                break
    return {h: c.most_common(1)[0][0] for h, c in votos.items() if c}


class Etiquetas:
    """Asigna IDs globales (1, 2, 3…) a las etiquetas locales de cada tramo."""

    def __init__(self):
        self.n = 0

    def nueva(self):
        self.n += 1
        return self.n

    def mapear(self, locales, mapa):
        return {h: mapa.get(h) or self.nueva() for h in sorted(set(locales))}


def unir(tramos, ids):
    """tramos: {k: palabras con etiqueta local}. Devuelve palabras con ID global."""
    total = []
    previo = []
    for k in sorted(tramos):
        ws = tramos[k]
        s0 = k * PASO
        corte = s0 + (LARGO - PASO) / 2 if k else 0     # mitad del solape
        ref = [w for w in previo if w[0] >= s0]
        mapa = votar([w for w in ws if w[0] < s0 + (LARGO - PASO)], ref)
        glob = ids.mapear([w[3] for w in ws], mapa)
        etiquetadas = [(a, b, w, glob[h]) for a, b, w, h in ws]
        total = [w for w in total if w[0] < corte] + [w for w in etiquetadas if w[0] >= corte]
        previo = etiquetadas
    return total


def huecos(ws, dur):
    """Minutos sin palabras rodeados de habla (chirp_3 los omite sin avisar)."""
    por_min = Counter(int(w[0] // 60) for w in ws)
    n = math.ceil(dur / 60)
    malos = [m for m in range(1, n - 1)
             if por_min[m] < 3 and por_min[m - 1] > 40 and por_min[m + 1] > 40]
    # agrupar minutos cercanos en un único clip
    grupos = []
    for m in malos:
        if grupos and (m + 1) * 60 - grupos[-1][0] <= PARCHE - 120:   # el clip debe cubrir el grupo con margen
            grupos[-1][1] = (m + 1) * 60
        else:
            grupos.append([m * 60, (m + 1) * 60])
    return grupos


def desordenes(json_path, offset, largo=LARGO, min_palabras=20):
    """Zonas donde chirp_3 retrocede en el tiempo y repite/reubica texto (orden crudo de salida)."""
    seq = [float(w["startOffset"].rstrip("s"))
           for r in json.load(open(json_path)).get("results", [])
           for w in (r.get("alternatives") or [{}])[0].get("words", []) if "startOffset" in w]
    seq = [x for x in seq if x <= largo + 1]          # inicios imposibles no cuentan
    zonas, mx, i = [], -1.0, 0
    while i < len(seq):
        if seq[i] < mx - 20:
            j = i
            while j < len(seq) and seq[j] < mx:
                j += 1
            if j - i >= min_palabras:
                zonas.append([offset + min(seq[i:j]), offset + mx])
            i = j
            continue
        mx = max(mx, seq[i])
        i += 1
    return zonas


def densidad_relativa(ws, h0, h1):
    """Palabras/s de la zona sobre la mediana de los minutos vecinos (±5 min).

    Calibrado en la nota del 01/10: zona con texto repetido ×1,41; ventanas sanas ×0,9–1,15.
    Devuelve None si los vecinos casi no tienen habla (no hay referencia para comparar).
    """
    d = lambda a, b: sum(1 for w in ws if a <= w[0] < b) / max(b - a, 1)
    vecinos = [d(a, a + 60) for a in range(int(h0) - 300, int(h1) + 300, 60) if not (h0 - 60 < a < h1)]
    mediana = sorted(vecinos)[len(vecinos) // 2] if vecinos else 0
    return d(h0, h1) / mediana if mediana >= 0.5 else None


def agrupar(zonas):
    """Une zonas cercanas mientras un clip de PARCHE las cubra con margen."""
    grupos = []
    for a, b in sorted(zonas):
        if grupos and b - grupos[-1][0] <= PARCHE - 120 and a - grupos[-1][1] < PARCHE / 2:
            grupos[-1][1] = max(grupos[-1][1], b)
        else:
            grupos.append([a, b])
    return grupos


def parchear(ws, parches, ids):
    """parches: [(ini_hueco, fin_hueco, palabras_clip)]. Inserta palabras solo dentro del hueco."""
    for h0, h1, clip in parches:
        fuera = [w for w in clip if not (h0 - 5 <= w[0] < h1 + 5)]
        mapa = votar(fuera, [w for w in ws if not (h0 <= w[0] < h1)])
        glob = ids.mapear([w[3] for w in clip], mapa)
        dentro = [(a, b, w, glob[h]) for a, b, w, h in clip if h0 <= a < h1]
        ws = sorted([w for w in ws if not (h0 <= w[0] < h1)] + dentro)
    return ws


# ---------------------------------------------------------------- salida

def bloques(ws):
    """Agrupa palabras contiguas del mismo hablante (corta en pausas >2 s o 30 s de largo)."""
    out = []
    for a, b, w, h in ws:
        if out and out[-1]["h"] == h and a - out[-1]["fin"] < 2 and a - out[-1]["ini"] < 30:
            out[-1]["fin"] = b
            out[-1]["txt"].append(w)
        else:
            out.append({"ini": a, "fin": b, "h": h, "txt": [w]})
    return out


def renumerar(ws):
    orden = {}
    for w in ws:
        orden.setdefault(w[3], len(orden) + 1)
    return [(a, b, w, orden[h]) for a, b, w, h in ws]


CABECERA = """Transcripción automática (Google STT chirp_3 + diarización).
Los hablantes están numerados, no identificados por nombre. El número es confiable dentro de un
mismo tramo de conversación, pero puede cambiar si alguien deja de hablar mucho tiempo: la misma
persona puede aparecer con otro número más adelante.
"""


def escribir(ws, carpeta):
    bl = bloques(renumerar(ws))
    with open(carpeta / "transcripcion.srt", "w") as f:
        for i, b in enumerate(bl, 1):
            f.write(f"{i}\n{hms(b['ini'], True)} --> {hms(b['fin'], True)}\n"
                    f"[Hablante {b['h']}] {' '.join(b['txt'])}\n\n")
    with open(carpeta / "transcripcion.txt", "w") as f:
        f.write(CABECERA + "\n")
        previo = None
        for b in bl:
            if b["h"] != previo:
                f.write(f"\n[{hms(b['ini'])}] Hablante {b['h']}:\n")
                previo = b["h"]
            f.write(" ".join(b["txt"]) + "\n")
    return len(bl), len({b["h"] for b in bl})


def finalizar(fuente, carpeta, dur, n_palabras, n_hablantes):
    """Audio reproducible en el navegador + metadatos para el visor; borra intermedios pesados."""
    audio = carpeta / "audio.m4a"
    if not audio.exists():
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(fuente), "-map", "0:a:0",
                            "-c:a", "copy", str(audio)], capture_output=True, text=True)
        if r.returncode != 0:   # códec no copiable a m4a: recodificar
            sh("ffmpeg", "-v", "error", "-y", "-i", str(fuente), "-map", "0:a:0", "-c:a", "aac",
               "-b:a", "96k", str(audio))
    meta_path = carpeta / "meta.json"
    meta = json.load(open(meta_path)) if meta_path.exists() else {}
    meta.update({"duracion": round(dur, 1), "palabras": n_palabras, "hablantes": n_hablantes,
                 "transcripto": time.strftime("%Y-%m-%d %H:%M"), "motor": f"chirp_3 {IDIOMA}"})
    meta.setdefault("titulo", carpeta.name)
    json.dump(meta, open(meta_path, "w"), ensure_ascii=False, indent=2)
    # se conservan los JSON de STT (trabajo/salida, parches) para poder re-armar sin pagar de nuevo
    trabajo = carpeta / "trabajo"
    shutil.rmtree(trabajo / "tramos", ignore_errors=True)
    for f in [trabajo / "audio.flac", *(trabajo / "parches").glob("*.flac")]:
        f.unlink(missing_ok=True)


# ---------------------------------------------------------------- orquestación

def run(fuente, nombre, conservar_bucket=False, con_resumen=True):
    carpeta = OUTPUT / nombre
    trabajo = carpeta / "trabajo"
    trabajo.mkdir(parents=True, exist_ok=True)

    flac = trabajo / "audio.flac"
    if not flac.exists():
        print("1. Convirtiendo a FLAC mono 16 kHz…", flush=True)
        sh("ffmpeg", "-v", "error", "-y", "-i", str(fuente), "-map", "0:a:0",
           "-ac", "1", "-ar", "16000", "-c:a", "flac", str(flac))
    dur = duracion(flac)
    n = max(1, math.ceil((dur - (LARGO - PASO)) / PASO))
    print(f"   {dur / 60:.1f} min -> {n} tramo(s)")

    tramos_dir = trabajo / "tramos"
    tramos_dir.mkdir(exist_ok=True)
    archivos = []
    for k in range(n):
        t = tramos_dir / f"t_{k:02d}.flac"
        if not t.exists():
            cortar(flac, t, k * PASO, LARGO)
        archivos.append(t)

    bucket = f"{proyecto()}-stt-{re.sub(r'[^a-z0-9-]', '-', nombre.lower())}"[:63].rstrip("-")
    salida_dir = trabajo / "salida"
    try:
        if not all(list(salida_dir.glob(f"{a.stem}_transcript_*.json")) for a in archivos):
            print(f"2. Bucket privado gs://{bucket} y transcripción…", flush=True)
            salida_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["gcloud", "storage", "buckets", "create", f"gs://{bucket}", "--project", proyecto(),
                            "--location", REGION.upper(), "--uniform-bucket-level-access",
                            "--public-access-prevention"], capture_output=True, text=True)
            faltan = [a for a in archivos if not list(salida_dir.glob(f"{a.stem}_transcript_*.json"))]
            uris = subir(faltan, bucket, "tramos")
            try:
                reconocer(uris, bucket, "out")
            finally:
                subprocess.run(["gcloud", "storage", "cp", f"gs://{bucket}/out/*", str(salida_dir)],
                               capture_output=True, text=True)

        print("3. Uniendo tramos y encadenando hablantes…", flush=True)
        tramos = {k: palabras(json_de(salida_dir, a.stem), k * PASO) for k, a in enumerate(archivos)}
        ids = Etiquetas()
        ws = unir(tramos, ids)

        desorden = [z for k, a in enumerate(archivos) for z in desordenes(json_de(salida_dir, a.stem), k * PASO)]
        grupos = agrupar(huecos(ws, dur) + desorden)
        if grupos:
            print(f"4. {len(grupos)} zona(s) a corregir ({len(desorden)} con texto desordenado/repetido): "
                  + ", ".join(f"{hms(a)}–{hms(b)}" for a, b in grupos), flush=True)
            print(f"   re-transcribiendo clips de {PARCHE // 60} min…", flush=True)
            parches_dir = trabajo / "parches"
            parches_dir.mkdir(exist_ok=True)
            clips = []
            for h0, h1 in grupos:
                ini = max(0, (h0 + h1) / 2 - PARCHE / 2)
                c = parches_dir / f"x_{int(ini)}.flac"
                if not c.exists():
                    cortar(flac, c, ini, PARCHE)
                clips.append((h0, h1, ini, c))
            pend = [c for *_, c in clips if not list(parches_dir.glob(f"{c.stem}_transcript_*.json"))]
            if pend:
                subprocess.run(["gcloud", "storage", "buckets", "create", f"gs://{bucket}", "--project", proyecto(),
                                "--location", REGION.upper(), "--uniform-bucket-level-access",
                                "--public-access-prevention"], capture_output=True, text=True)
                reconocer(subir(pend, bucket, "parches"), bucket, "parchesout")
                bajar(bucket, "parchesout", parches_dir)
            ws = parchear(ws, [(h0, h1, palabras(json_de(parches_dir, c.stem), ini, PARCHE)) for h0, h1, ini, c in clips], ids)
            for h0, h1, _, _ in clips:
                r = densidad_relativa(ws, h0, h1)
                if r is None:
                    print(f"   · {hms(h0)}–{hms(h1)} corregido (poca habla alrededor para validar)")
                elif r > 1.25 or r < 0.3:
                    motivo = "posible texto repetido" if r > 1.25 else "posible texto faltante"
                    print(f"   ⚠ {hms(h0)}–{hms(h1)}: {motivo} tras corregir (densidad ×{r:.2f}); revisar a mano")
                else:
                    print(f"   ✓ {hms(h0)}–{hms(h1)} corregido (densidad ×{r:.2f})")
            quedan = huecos(ws, dur)
            if quedan:
                print(f"   ⚠ siguen {len(quedan)} hueco(s) sin texto: "
                      + ", ".join(f"{hms(a)}–{hms(b)}" for a, b in quedan))
        else:
            print("4. Sin huecos detectados.")

        nb, nh = escribir(ws, carpeta)
        print(f"5. {len(ws)} palabras, {nb} bloques, {nh} hablantes -> {carpeta}/transcripcion.txt|.srt")
        finalizar(fuente, carpeta, dur, len(ws), nh)
        if con_resumen:
            print("6. Resumen…", flush=True)
            try:
                import resumir
                resumir.resumir(carpeta)
            except Exception as e:   # la transcripción ya quedó; el resumen se puede reintentar aparte
                print(f"   ⚠ no se generó el resumen: {e}\n"
                      f"     reintentar con: python3 resumir.py {carpeta.name}")
    finally:
        if not conservar_bucket:
            r = subprocess.run(["gcloud", "storage", "rm", "-r", f"gs://{bucket}"], capture_output=True, text=True)
            if r.returncode == 0:
                print(f"   bucket gs://{bucket} eliminado")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("listar")
    r = sub.add_parser("run")
    r.add_argument("audio", help="ruta a un audio o índice de `listar`")
    r.add_argument("--nombre", help="nombre de la carpeta de salida")
    r.add_argument("--conservar-bucket", action="store_true")
    r.add_argument("--sin-resumen", action="store_true", help="no generar resumen.md con Claude")
    a = p.parse_args()

    if a.cmd == "listar":
        return listar()
    if a.audio.isdigit():
        etq, fecha, _, ruta = notas_de_voz()[int(a.audio)]
        nombre = a.nombre or ("nota-" + fecha.replace(" ", "_").replace(":", "")[:15])
        titulo, origen = f"Nota de voz {fecha[:16]}", "Notas de Voz"
    else:
        ruta = Path(a.audio).expanduser()
        nota = next((x for x in notas_de_voz() if x[3].resolve() == ruta.resolve()), None)
        if nota:    # ruta dentro de Notas de Voz: mismo nombre y título que por índice
            fecha = nota[1]
            nombre = a.nombre or ("nota-" + fecha.replace(" ", "_").replace(":", "")[:15])
            titulo, origen = f"Nota de voz {fecha[:16]}", "Notas de Voz"
        else:
            nombre = a.nombre or re.sub(r"[^a-z0-9]+", "-", ruta.stem.lower()).strip("-")
            titulo, origen = ruta.stem, str(ruta.parent)
    if not ruta.exists():
        sys.exit(f"no existe {ruta}")
    # trabajar sobre una copia, nunca dentro del contenedor de Notas de Voz
    carpeta = OUTPUT / nombre
    carpeta.mkdir(parents=True, exist_ok=True)
    meta_path = carpeta / "meta.json"
    if not meta_path.exists():
        json.dump({"titulo": titulo, "origen": origen, "archivo": ruta.name,
                   "grabado": time.strftime("%Y-%m-%d %H:%M", time.localtime(ruta.stat().st_mtime))},
                  open(meta_path, "w"), ensure_ascii=False, indent=2)
    copia = carpeta / ("original" + ruta.suffix)
    if not copia.exists():
        shutil.copy2(ruta, copia)
    run(copia, nombre, a.conservar_bucket, not a.sin_resumen)


if __name__ == "__main__":
    main()
