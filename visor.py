#!/usr/bin/env python3
"""Visor local de transcripciones: lista, reproduce el audio sincronizado y descarga TXT/SRT.

    python3 visor.py            # http://127.0.0.1:8765
    python3 visor.py --puerto 9000

Solo escucha en 127.0.0.1 (las transcripciones pueden tener datos sensibles).
Lee cada subcarpeta de output/ que tenga transcripcion.srt.
Los nombres de hablantes se guardan en <carpeta>/hablantes.json.
"""
import argparse
import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from config import OUTPUT

AUDIOS = ("audio.m4a", "audio.mp3", "audio.wav")
TIPOS = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".wav": "audio/wav",
         ".txt": "text/plain; charset=utf-8", ".srt": "text/plain; charset=utf-8", ".md": "text/markdown; charset=utf-8"}
ID_OK = re.compile(r"^[\w.\-]+$")
SRT_TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")
SRT_HABL = re.compile(r"^\[Hablante (\d+)\]\s*")


def carpeta(id_):
    if not ID_OK.match(id_ or ""):
        return None
    c = OUTPUT / id_
    return c if c.is_dir() and (c / "transcripcion.srt").exists() else None


def audio_de(c):
    return next((c / a for a in AUDIOS if (c / a).exists()), None)


def leer_json(p, defecto):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return defecto


def seg(ts):
    h, m, s, ms = map(int, SRT_TS.match(ts).groups())
    return h * 3600 + m * 60 + s + ms / 1000


def segmentos(c):
    out = []
    for bloque in c.joinpath("transcripcion.srt").read_text().strip().split("\n\n"):
        lineas = bloque.strip().split("\n")
        if len(lineas) < 3 or "-->" not in lineas[1]:
            continue
        a, b = (x.strip() for x in lineas[1].split("-->"))
        texto = " ".join(lineas[2:])
        m = SRT_HABL.match(texto)
        out.append({"i": seg(a), "f": seg(b), "h": int(m.group(1)) if m else 0,
                    "t": texto[m.end():] if m else texto})
    return out


def listado():
    items = []
    if not OUTPUT.is_dir():
        return []
    for c in sorted(OUTPUT.iterdir()):
        if not carpeta(c.name):
            continue
        meta = leer_json(c / "meta.json", {})
        segs = segmentos(c)
        items.append({
            "id": c.name,
            "titulo": meta.get("titulo", c.name),
            "origen": meta.get("origen", ""),
            "grabado": meta.get("grabado", ""),
            "transcripto": meta.get("transcripto", ""),
            "duracion": meta.get("duracion") or (segs[-1]["f"] if segs else 0),
            "hablantes": meta.get("hablantes") or len({s["h"] for s in segs}),
            "palabras": meta.get("palabras") or sum(len(s["t"].split()) for s in segs),
            "audio": bool(audio_de(c)),
            "resumen": (c / "resumen.md").exists(),
        })
    return sorted(items, key=lambda x: x["grabado"] or x["transcripto"], reverse=True)


def hms(s):
    s = int(s)
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


def txt_con_nombres(c):
    nombres = leer_json(c / "hablantes.json", {})
    meta = leer_json(c / "meta.json", {})
    lineas, previo = [f"{meta.get('titulo', c.name)}", "Transcripción automática (Google STT chirp_3 + diarización).", ""], None
    for s in segmentos(c):
        if s["h"] != previo:
            quien = nombres.get(str(s["h"])) or f"Hablante {s['h']}"
            lineas += ["", f"[{hms(s['i'])}] {quien}:"]
            previo = s["h"]
        lineas.append(s["t"])
    return "\n".join(lineas) + "\n"


class Handler(BaseHTTPRequestHandler):
    server_version = "visor"

    def log_message(self, fmt, *args):      # sin rutas ni contenido en consola
        pass

    def enviar(self, cuerpo, tipo, estado=HTTPStatus.OK, extra=None):
        datos = cuerpo.encode() if isinstance(cuerpo, str) else cuerpo
        self.send_response(estado)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(datos)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(datos)

    def json(self, obj, estado=HTTPStatus.OK):
        self.enviar(json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", estado)

    def no(self, estado=HTTPStatus.NOT_FOUND):
        self.json({"error": estado.phrase}, estado)

    def archivo(self, p, descarga=None):
        """Sirve un archivo con soporte de Range (necesario para adelantar el audio)."""
        tam = p.stat().st_size
        tipo = TIPOS.get(p.suffix, "application/octet-stream")
        rango = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        ini, fin = 0, tam - 1
        if rango and (rango.group(1) or rango.group(2)):
            if rango.group(1):
                ini = int(rango.group(1))
                fin = int(rango.group(2)) if rango.group(2) else tam - 1
            else:
                ini = max(0, tam - int(rango.group(2)))
            fin = min(fin, tam - 1)
            if ini > fin:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{tam}")
                self.end_headers()
                return
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Range", f"bytes {ini}-{fin}/{tam}")
        else:
            self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", tipo)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(fin - ini + 1))
        if descarga:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(descarga)}")
        self.end_headers()
        with open(p, "rb") as f:
            f.seek(ini)
            restante = fin - ini + 1
            try:
                while restante > 0:
                    bloque = f.read(min(1 << 16, restante))
                    if not bloque:
                        break
                    self.wfile.write(bloque)
                    restante -= len(bloque)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self):
        partes = [unquote(x) for x in urlparse(self.path).path.strip("/").split("/") if x]
        if partes == ["favicon.ico"]:
            return self.enviar(b"", "image/x-icon", HTTPStatus.NO_CONTENT)
        if not partes:
            return self.enviar(PAGINA, "text/html; charset=utf-8")
        if partes == ["api", "lista"]:
            return self.json(listado())
        if len(partes) != 3:
            return self.no()
        tipo, id_, recurso = partes
        c = carpeta(id_)
        if not c:
            return self.no()
        titulo = re.sub(r"[^\w\- ]+", "", leer_json(c / "meta.json", {}).get("titulo", id_)).strip() or id_
        if tipo == "api" and recurso == "transcripcion":
            r = c / "resumen.md"
            return self.json({"segmentos": segmentos(c), "nombres": leer_json(c / "hablantes.json", {}),
                              "resumen": r.read_text() if r.exists() else None})
        if tipo == "audio" and recurso == "stream":
            a = audio_de(c)
            return self.archivo(a) if a else self.no()
        if tipo == "descargar":
            if recurso == "txt":
                return self.enviar(txt_con_nombres(c), TIPOS[".txt"],
                                   extra={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(titulo + '.txt')}"})
            if recurso == "resumen" and (c / "resumen.md").exists():
                return self.archivo(c / "resumen.md", titulo + " - resumen.md")
            if recurso == "srt":
                return self.archivo(c / "transcripcion.srt", titulo + ".srt")
            if recurso == "audio" and audio_de(c):
                a = audio_de(c)
                return self.archivo(a, titulo + a.suffix)
        self.no()

    def do_POST(self):
        partes = [unquote(x) for x in urlparse(self.path).path.strip("/").split("/") if x]
        if len(partes) != 3 or partes[0] != "api" or partes[2] != "hablantes":
            return self.no()
        c = carpeta(partes[1])
        if not c:
            return self.no()
        largo = int(self.headers.get("Content-Length") or 0)
        if largo > 10_000:
            return self.no(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        try:
            datos = json.loads(self.rfile.read(largo) or "{}")
            assert isinstance(datos, dict)
            limpio = {str(int(k)): str(v).strip()[:60] for k, v in datos.items() if str(v).strip()}
        except (ValueError, AssertionError):
            return self.no(HTTPStatus.BAD_REQUEST)
        (c / "hablantes.json").write_text(json.dumps(limpio, ensure_ascii=False, indent=2))
        self.json({"ok": True, "nombres": limpio})


PAGINA = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Transcripciones</title>
<style>
:root{--bg:#f6f6f4;--panel:#fff;--txt:#1d1d1b;--sub:#6b6b66;--borde:#e3e2dd;--acento:#2f6fdb;--act:#eaf1fd;--hl:#fff3b0}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--panel:#1d1d1b;--txt:#ececea;--sub:#9b9a95;--borde:#2f2f2c;--acento:#7aa7f5;--act:#1f2b40;--hl:#5a4d10}}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--txt);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;display:grid;grid-template-columns:320px 1fr;height:100vh}
aside{border-right:1px solid var(--borde);overflow:auto;background:var(--panel)}
aside h1{font-size:15px;margin:0;padding:16px;border-bottom:1px solid var(--borde)}
.item{padding:12px 16px;border-bottom:1px solid var(--borde);cursor:pointer}
.item:hover{background:var(--bg)}.item.sel{background:var(--act)}
.item b{display:block;font-weight:600}.item small{color:var(--sub)}
main{display:flex;flex-direction:column;min-width:0;overflow:hidden}
header{padding:16px 24px;border-bottom:1px solid var(--borde);background:var(--panel)}
header h2{margin:0 0 4px;font-size:18px}.meta{color:var(--sub);font-size:13px}
.barra{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px}
audio{width:100%;margin-top:12px}
a.btn,button{font:inherit;font-size:13px;padding:6px 12px;border:1px solid var(--borde);border-radius:6px;background:var(--bg);color:var(--txt);text-decoration:none;cursor:pointer}
a.btn:hover,button:hover{border-color:var(--acento)}
input[type=search]{font:inherit;font-size:13px;padding:6px 10px;border:1px solid var(--borde);border-radius:6px;background:var(--bg);color:var(--txt);min-width:200px;flex:1}
label.chk{font-size:13px;color:var(--sub);display:flex;gap:4px;align-items:center}
#hablantes{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;max-height:30vh;overflow:auto}
summary{cursor:pointer;font-size:13px;color:var(--sub);margin-top:10px}
#hablantes input{font:inherit;font-size:12px;padding:3px 8px;border-radius:12px;border:1px solid var(--borde);background:var(--bg);color:var(--txt);width:130px}
#texto,#resumen{overflow:auto;padding:16px 24px 80px;flex:1}
.tabs{display:flex;gap:4px;padding:0 24px;border-bottom:1px solid var(--borde);background:var(--panel)}
.tabs button{border:none;border-bottom:2px solid transparent;border-radius:0;background:none;padding:10px 12px;color:var(--sub)}
.tabs button.on{border-bottom-color:var(--acento);color:var(--txt);font-weight:600}
#resumen{max-width:860px;line-height:1.6}#resumen h1{font-size:20px;margin:4px 0 8px}#resumen h2{font-size:16px;margin:22px 0 6px}
#resumen h3{font-size:14px;margin:16px 0 4px}#resumen ul{margin:4px 0;padding-left:20px}#resumen li{margin:3px 0}#resumen p{margin:6px 0}
a.ts-link{color:var(--acento);font-variant-numeric:tabular-nums;text-decoration:none;cursor:pointer}a.ts-link:hover{text-decoration:underline}
.seg{display:grid;grid-template-columns:72px 1fr;gap:12px;padding:6px 8px;border-radius:6px;cursor:pointer}
.seg:hover{background:var(--bg)}.seg.act{background:var(--act)}
.ts{color:var(--sub);font-variant-numeric:tabular-nums;font-size:13px;padding-top:2px}
.quien{font-weight:600;font-size:13px;margin-right:6px}
mark{background:var(--hl);color:inherit}
.vacio{color:var(--sub);padding:40px 24px}
@media (max-width:760px){body{grid-template-columns:1fr;grid-template-rows:auto 1fr;height:auto}aside{max-height:35vh}.seg{grid-template-columns:60px 1fr}}
</style></head><body>
<aside><h1>Transcripciones</h1><div id="lista"></div></aside>
<main><div class="vacio" id="vacio">Elegí una transcripción.</div>
<header id="cab" hidden><h2 id="titulo"></h2><div class="meta" id="meta"></div>
<audio id="audio" controls preload="metadata"></audio>
<div class="barra"><input type="search" id="buscar" placeholder="Buscar en el texto…">
<label class="chk"><input type="checkbox" id="seguir" checked> seguir audio</label>
<a class="btn" id="dres">Resumen</a><a class="btn" id="dtxt">Descargar TXT</a><a class="btn" id="dsrt">SRT</a><a class="btn" id="daud">Audio</a></div>
<details id="habl"><summary id="hablsum">Hablantes</summary><div id="hablantes"></div></details></header>
<nav class="tabs" id="tabs" hidden><button data-tab="resumen">Resumen</button><button data-tab="texto">Transcripción</button></nav>
<div id="resumen" hidden></div><div id="texto" hidden></div></main>
<script>
const $=s=>document.querySelector(s), COL=["#c0392b","#2f6fdb","#1e8449","#a04000","#7d3c98","#117a65","#b7950b","#5d6d7e"];
const fmt=s=>{s=Math.floor(s);return [s/3600|0,(s/60|0)%60,s%60].map(x=>String(x).padStart(2,"0")).join(":")};
const esc=s=>s.replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let actual=null, segs=[], nombres={}, nodos=[], idxAct=-1;
async function cargarLista(){
  const l=await (await fetch("/api/lista")).json();
  $("#lista").innerHTML=l.length?l.map(x=>`<div class="item" data-id="${esc(x.id)}"><b>${esc(x.titulo)}</b>
    <small>${fmt(x.duracion)} · ${x.hablantes} hablantes · ${x.palabras.toLocaleString("es")} palabras<br>${esc(x.grabado||x.transcripto||"")}${x.audio?"":" · sin audio"}${x.resumen?" · resumen":""}</small></div>`).join("")
    :'<div class="vacio">Todavía no hay transcripciones.</div>';
  document.querySelectorAll(".item").forEach(e=>e.onclick=()=>abrir(l.find(x=>x.id===e.dataset.id)));
  const h=decodeURIComponent(location.hash.slice(1)); const ini=l.find(x=>x.id===h); if(ini) abrir(ini);
}
const nombre=h=>nombres[h]||`Hablante ${h}`;
async function abrir(x){
  actual=x; location.hash=encodeURIComponent(x.id);
  document.querySelectorAll(".item").forEach(e=>e.classList.toggle("sel",e.dataset.id===x.id));
  $("#vacio").hidden=true; $("#cab").hidden=false; $("#texto").hidden=false;
  $("#titulo").textContent=x.titulo;
  $("#meta").textContent=[x.origen,x.grabado&&`grabado ${x.grabado}`,x.transcripto&&`transcripto ${x.transcripto}`].filter(Boolean).join(" · ");
  const id=encodeURIComponent(x.id);
  $("#audio").hidden=!x.audio; $("#daud").hidden=!x.audio; if(x.audio) $("#audio").src=`/audio/${id}/stream`; else $("#audio").removeAttribute("src");
  $("#dtxt").href=`/descargar/${id}/txt`; $("#dsrt").href=`/descargar/${id}/srt`; $("#daud").href=`/descargar/${id}/audio`;
  const r=await (await fetch(`/api/${id}/transcripcion`)).json();
  segs=r.segmentos; nombres=r.nombres||{}; $("#buscar").value=""; render(); renderHablantes();
  $("#resumen").innerHTML=r.resumen?md(r.resumen):'<div class="vacio">Todavía no hay resumen para esta transcripción.</div>';
  $("#dres").hidden=!r.resumen; $("#dres").href=`/descargar/${id}/resumen`; $("#tabs").hidden=false;
  pestana(r.resumen?"resumen":"texto");
}
function renderHablantes(){
  const hs=[...new Set(segs.map(s=>s.h))].sort((a,b)=>a-b);
  $("#hablsum").textContent=`Hablantes (${hs.length}) — escribí un nombre para renombrar`; $("#habl").open=hs.length<=8;
  $("#hablantes").innerHTML=hs.map(h=>`<input data-h="${h}" style="border-color:${COL[(h-1)%COL.length]}" placeholder="Hablante ${h}" value="${esc(nombres[h]||"")}" title="Renombrar hablante ${h}">`).join("");
  document.querySelectorAll("#hablantes input").forEach(i=>i.onchange=async()=>{
    nombres[i.dataset.h]=i.value.trim();
    const r=await fetch(`/api/${encodeURIComponent(actual.id)}/hablantes`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(nombres)});
    if(r.ok){nombres=(await r.json()).nombres; render();}
  });
}
function pestana(t){
  document.querySelectorAll("#tabs button").forEach(b=>b.classList.toggle("on",b.dataset.tab===t));
  $("#resumen").hidden=t!=="resumen"; $("#texto").hidden=t!=="texto";
}
document.querySelectorAll("#tabs button").forEach(b=>b.onclick=()=>pestana(b.dataset.tab));
function saltar(seg){const a=$("#audio"); if(!actual?.audio) return; a.currentTime=seg; a.play();}
// Markdown mínimo y seguro: se escapa todo y solo se interpretan #, viñetas, **negrita** y [hh:mm:ss]
function md(src){
  const inl=t=>esc(t).replace(/\*\*(.+?)\*\*/g,"<b>$1</b>")
    .replace(/\[(\d{1,2}):(\d{2}):(\d{2})\]/g,(m,h,mi,se)=>`<a class="ts-link" data-s="${+h*3600+ +mi*60+ +se}">${m.slice(1,-1)}</a>`);
  let out="",lista=false;
  for(const l of src.split("\n")){
    const t=l.trim(), m=t.match(/^(#{1,3})\s+(.*)$/), li=t.match(/^[-*]\s+(.*)$/);
    if(li){ if(!lista){out+="<ul>";lista=true} out+=`<li>${inl(li[1])}</li>`; continue }
    if(lista){out+="</ul>";lista=false}
    if(m) out+=`<h${m[1].length}>${inl(m[2])}</h${m[1].length}>`; else if(t) out+=`<p>${inl(t)}</p>`;
  }
  return out+(lista?"</ul>":"");
}
$("#resumen").onclick=e=>{const a=e.target.closest(".ts-link"); if(a) saltar(+a.dataset.s);};
function render(){
  const q=$("#buscar").value.trim().toLowerCase(); let prev=null;
  const html=segs.map((s,i)=>{
    if(q && !s.t.toLowerCase().includes(q) && !nombre(s.h).toLowerCase().includes(q)) return "";
    let t=esc(s.t); if(q) t=t.replace(new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g,"\\$&"),"gi"),m=>`<mark>${m}</mark>`);
    const q2=(s.h!==prev||q)?`<span class="quien" style="color:${COL[(s.h-1)%COL.length]}">${esc(nombre(s.h))}</span>`:""; prev=s.h;
    return `<div class="seg" data-i="${i}"><span class="ts">${fmt(s.i)}</span><div>${q2}${t}</div></div>`;
  }).join("");
  $("#texto").innerHTML=html||'<div class="vacio">Sin coincidencias.</div>';
  nodos=[]; document.querySelectorAll(".seg").forEach(e=>{nodos[+e.dataset.i]=e; e.onclick=()=>{const a=$("#audio"); if(!actual.audio)return; a.currentTime=segs[+e.dataset.i].i; a.play();}});
  idxAct=-1;
}
$("#buscar").oninput=render;
$("#audio").ontimeupdate=()=>{
  const t=$("#audio").currentTime; let lo=0,hi=segs.length-1,k=-1;
  while(lo<=hi){const m=(lo+hi)>>1; if(segs[m].i<=t){k=m;lo=m+1}else hi=m-1}
  if(k===idxAct) return; nodos[idxAct]?.classList.remove("act"); idxAct=k; const n=nodos[k];
  if(n){n.classList.add("act"); if($("#seguir").checked) n.scrollIntoView({block:"center",behavior:"smooth"});}
};
cargarLista();
</script></body></html>"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--puerto", type=int, default=8765)
    a = p.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.puerto), Handler)
    print(f"Visor en http://127.0.0.1:{a.puerto}  (Ctrl+C para cortar)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
