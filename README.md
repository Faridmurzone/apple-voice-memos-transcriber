# Apple Voice Memos Transcriber

Transcribe notas de voz de macOS (app **Notas de Voz** / Voice Memos) o cualquier audio, con
**diarización** (quién habla), genera un **resumen accionable con Claude** y trae un **visor local**
para leer la transcripción sincronizada con el audio.

- **Transcripción:** Google Cloud Speech-to-Text v2, modelo `chirp_3`, español (`es-US`).
- **Audios largos:** corta en tramos de 19 min con solape, une los hablantes entre tramos y
  re-transcribe los huecos que `chirp_3` a veces omite. Es reanudable: si se corta, se vuelve a
  correr y no repite lo ya pagado.
- **Resumen:** `resumen.md` con participantes, decisiones, próximos pasos y timestamps clicables.
- **Visor:** lista de transcripciones, reproductor sincronizado, búsqueda, renombrar hablantes y
  descarga TXT/SRT. Solo escucha en `127.0.0.1`.

Todo lo que se genera queda en `output/`, que está en `.gitignore`.

## Requisitos

- macOS (para leer Notas de Voz; con `run <ruta>` sirve cualquier sistema con las herramientas de abajo)
- Python 3.10+
- [ffmpeg](https://ffmpeg.org/): `brew install ffmpeg`
- [Google Cloud CLI](https://cloud.google.com/sdk/docs/install): `brew install --cask google-cloud-sdk`
- Un proyecto de GCP con la API **Speech-to-Text** habilitada y permisos para crear buckets de
  Cloud Storage y llamar a STT. **Los costos de STT y GCS se facturan a ese proyecto.**
- (Opcional, para el resumen) una API key de [Anthropic](https://console.anthropic.com/).

## Configuración (una sola vez)

```bash
git clone https://github.com/Faridmurzone/apple-voice-memos-transcriber.git
cd apple-voice-memos-transcriber

make setup                 # crea .venv, instala dependencias y copia .env.example -> .env
```

Editá `.env`:

```bash
GCP_PROYECTO=mi-proyecto-gcp        # obligatorio
ANTHROPIC_API_KEY=sk-ant-...        # para el resumen (o TRANSCRIPCIONES_SECRETO, ver abajo)
```

Autenticá gcloud con tu usuario (los scripts usan `gcloud auth print-access-token`) y habilitá la API:

```bash
gcloud auth login
gcloud services enable speech.googleapis.com storage.googleapis.com --project mi-proyecto-gcp
```

Para leer la app Notas de Voz, la terminal necesita **Acceso total al disco**:
*Ajustes del Sistema → Privacidad y seguridad → Acceso total al disco →* activá Terminal / iTerm / tu IDE
y reiniciala.

## Uso

```bash
make listar                              # notas de voz disponibles, con su índice
make transcribir AUDIO=0                 # transcribe la nota 0 de la lista (+ resumen)
make transcribir AUDIO=~/Downloads/reunion.m4a NOMBRE=reunion-equipo   # cualquier archivo
make visor                               # abre http://127.0.0.1:8765
```

Cada audio queda en su carpeta:

```
output/<nombre>/
├── transcripcion.txt   # texto con [hh:mm:ss] Hablante N
├── transcripcion.srt   # subtítulos
├── resumen.md          # resumen generado con Claude
├── audio.m4a           # copia reproducible en el navegador
├── meta.json           # duración, hablantes, costo estimado del resumen…
└── trabajo/            # JSON crudos de STT (permiten re-armar sin volver a pagar)
```

### Comandos directos (sin make)

```bash
source .venv/bin/activate
python transcribir.py listar
python transcribir.py run <n°|ruta> [--nombre X] [--sin-resumen] [--conservar-bucket]
python resumir.py <carpeta> [--forzar]   # (re)genera el resumen de output/<carpeta>
python resumir.py --pendientes           # resume todas las que no tengan resumen.md
python visor.py [--puerto 9000]
```

## Variables de `.env`

| Variable | Obligatoria | Qué hace |
|---|---|---|
| `GCP_PROYECTO` | Sí | Proyecto de GCP para STT y los buckets temporales |
| `ANTHROPIC_API_KEY` | Para el resumen | API key de Anthropic |
| `TRANSCRIPCIONES_SECRETO` | Alternativa a la key | Nombre de un secreto de Secret Manager (en `GCP_PROYECTO`) con la key; se lee en el momento y solo queda en memoria |
| `ORGANIZACION` | No | Contexto para el resumen, p. ej. `ACME, una empresa de software de salud`. Agrega la sección "Relevante para ACME" |
| `TRANSCRIPCIONES_OUTPUT` | No | Carpeta de salida (por defecto `./output`) |

Sin key de Anthropic igual podés transcribir: usá `--sin-resumen`, o generá el resumen después con
`make resumir`.

## Privacidad

- El audio se sube a un **bucket privado temporal** (acceso uniforme, sin acceso público) que se
  borra al terminar, salvo `--conservar-bucket`.
- No se loguea contenido de las transcripciones, solo conteos.
- El prompt del resumen le pide al modelo no copiar datos que identifiquen a pacientes o terceros.
- `output/` y `.env` nunca se commitean. Si usás el repo para otra cosa, revisá `git status` antes de pushear.

## Problemas comunes

| Síntoma | Solución |
|---|---|
| `no pude leer la base de Notas de Voz` | Dale Acceso total al disco a la terminal (ver arriba) o usá `run <ruta>` |
| `falta GCP_PROYECTO` | Completá `.env` |
| `API: ... permission` / `PERMISSION_DENIED` | `gcloud auth login` y verificá que tu usuario tenga permisos de STT y Storage en el proyecto |
| `no se generó el resumen` | La transcripción ya quedó; revisá la key y corré `python resumir.py <carpeta>` |
| SSL raros desde Python | Las llamadas HTTP van por `curl` justamente por esto; no hace falta hacer nada |
