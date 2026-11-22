"""Fake OrcaSlicer CLI for tests: write a small sliced 3MF and result.json."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from helpers import sliced_3mf  # noqa: E402

args = sys.argv[1:]
outdir = Path(args[args.index("--outputdir") + 1])
name = args[args.index("--export-3mf") + 1]
model = args[-1]
(outdir / "args.json").write_text(json.dumps(args))
if b"fail" in Path(model).read_bytes():
    (outdir / "result.json").write_text(json.dumps({"return_code": -100, "error_string": 'Object is outside the plate'}))
    print("error: object outside")
    sys.exit(1)
# At most four copies fit per plate; OrcaSlicer puts the rest on a second plate.
copies = int(args[args.index("--clone-objects") + 1]) if "--clone-objects" in args else 1
plates = ((1, [(1, "PLA", "#FF0000", "GFA00", 3.7)]), (2, [(1, "PLA", "#FF0000", "GFA00", 1.9)]))
(outdir / name).write_bytes(sliced_3mf(plates if copies > 4 else plates[:1]))
(outdir / "result.json").write_text(json.dumps({"return_code": 0, "warnings": []}))
