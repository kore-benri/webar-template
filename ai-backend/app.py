"""GLB Studio image-to-3D adapter for the official TripoSR CLI."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import trimesh
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

TRIPOSR_DIR = Path(os.environ.get("TRIPOSR_DIR", "./TripoSR")).resolve()
DEVICE = os.environ.get("TRIPOSR_DEVICE", "cpu")
ORIGINS = [x.strip() for x in os.environ.get(
    "ALLOWED_ORIGINS", "https://kore-benri.github.io,http://localhost:8000"
).split(",") if x.strip()]
MAX_IMAGE_BYTES = 8 * 1024 * 1024

app = FastAPI(title="GLB Studio TripoSR Adapter")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

@app.get("/health")
async def health():
    return {"ok": True, "triposr_installed": (TRIPOSR_DIR / "run.py").exists(), "device": DEVICE}

@app.post("/generate")
async def generate(image: UploadFile = File(...)):
    if not (TRIPOSR_DIR / "run.py").exists():
        raise HTTPException(503, "TripoSR not installed; see README")
    if image.content_type not in ("image/png", "image/jpeg", "image/webp"):
        raise HTTPException(415, "Upload PNG, JPEG or WebP")
    payload = await image.read(MAX_IMAGE_BYTES + 1)
    if len(payload) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "Image exceeds 8 MB")
    # PIL checks file validity before passing it to the model.
    from PIL import Image, UnidentifiedImageError
    try:
        import io
        with Image.open(io.BytesIO(payload)) as img:
            img.verify()
    except (UnidentifiedImageError, OSError):
        raise HTTPException(400, "Invalid image")
    with tempfile.TemporaryDirectory(prefix="glbstudio-") as folder:
        temp = Path(folder)
            input_file = temp / ("input.jpg" if image.content_type == "image/jpeg" else "input.png")
            if image.content_type == "image/webp":
                from PIL import Image
                import io
                Image.open(io.BytesIO(payload)).convert("RGBA").save(input_file)
            else:
                input_file.write_bytes(payload)
            output_dir = temp / "output"
            cmd = [
                sys.executable, str(TRIPOSR_DIR / "run.py"),
                str(input_file), "--output-dir", str(output_dir),
                "--device", DEVICE,
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(TRIPOSR_DIR),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=570)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                raise HTTPException(504, "3D generation timed out")
            if proc.returncode:
                raise HTTPException(502, "TripoSR failed: " + stderr.decode(errors="replace")[-1000:])
            meshes = list(output_dir.rglob("mesh.obj"))
            if not meshes:
                raise HTTPException(502, "TripoSR did not produce mesh.obj")
            scene = trimesh.load(meshes[0], force="scene")
            glb = scene.export(file_type="glb")
            return Response(content=glb, media_type="model/gltf-binary",
                            headers={"Content-Disposition": 'attachment; filename="generated.glb"'})
