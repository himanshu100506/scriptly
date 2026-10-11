import sys, time
from app.pipeline_core import run_pipeline
t = time.time()
try:
    r = run_pipeline(sys.argv[1])
except Exception as e:
    print("FAILED:", type(e).__name__, "-", e); sys.exit(1)
print("\nlanguage:", r["language"], "| duration:", r["metadata"]["duration"], "| wall:", round(time.time()-t,1))
print("timings:", r["timings"])
for k in ("english","hinglish"):
    print(f"\n--- {k} ---"); print(r[k][:500])
