import json
import sys
import os
from pathlib import Path
from faster_whisper import WhisperModel

model = WhisperModel(str(Path(os.environ.get('AGENT_HOME', str(Path.home()))) / 'speech/model'), device='cpu', compute_type='int8', cpu_threads=2, num_workers=1)
segments, info = model.transcribe(sys.argv[1], beam_size=3, vad_filter=False)
text = ''.join(segment.text for segment in segments).strip()
print(json.dumps({'text': text, 'language': info.language}, ensure_ascii=False))
