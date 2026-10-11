import sys, io
from app.pipeline_core import run_pipeline
label = sys.argv[2]
r = run_pipeline(sys.argv[1])
out = "=== TRANSCRIPT (Hindi, from Whisper) ===\n" + r["original_transcript"] + "\n\n=== HINGLISH ===\n" + r["hinglish"] + "\n\n=== ENGLISH ===\n" + r["english"] + "\n"
open(f"out_{label}.txt", "w", encoding="utf-8").write(out)
print("saved out_%s.txt | timings: %s" % (label, r["timings"]))
