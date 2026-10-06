PY := .venv/bin/python

.PHONY: setup listar transcribir resumir visor

setup:            ## Crea el entorno virtual, instala dependencias y el .env
	python3 -m venv .venv
	$(PY) -m pip install -q -r requirements.txt
	@test -f .env || (cp .env.example .env && echo "Creado .env: completá GCP_PROYECTO")

listar:           ## Lista las notas de voz de la app Notas de Voz
	$(PY) transcribir.py listar

transcribir:      ## make transcribir AUDIO=3 (índice de listar) o AUDIO=~/ruta/audio.m4a [NOMBRE=x]
	$(PY) transcribir.py run "$(AUDIO)" $(if $(NOMBRE),--nombre "$(NOMBRE)")

resumir:          ## Genera resumen.md para todas las transcripciones que no lo tengan
	$(PY) resumir.py --pendientes

visor:            ## Abre el visor en http://127.0.0.1:8765
	$(PY) visor.py
