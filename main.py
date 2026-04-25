import base64
import asyncio
import json

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(
    title="Image Editor API",
    version="1.1.0",
    description="AI image editing service powered by AIEnhancer.",
)

# ✅ CORS (fully open)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

AES_KEY = b"ai-enhancer-web__aes-key"
AES_IV = b"aienhancer-aesiv"

HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://aienhancer.ai",
    "Referer": "https://aienhancer.ai/ai-image-editor",
}


# -------------------- MODELS --------------------

class EditRequest(BaseModel):
    image: str = Field(...)
    prompt: str = Field(...)
    filename: str = Field("input.jpg")


class EditResponse(BaseModel):
    success: bool
    job_id: str
    image: str


# -------------------- UTILS --------------------

def encrypt(obj: dict) -> str:
    plaintext = json.dumps(obj).encode("utf-8")
    padded = pad(plaintext, AES.block_size)
    cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_IV)
    encrypted = cipher.encrypt(padded)
    return base64.b64encode(encrypted).decode("utf-8")


# -------------------- NSFW --------------------

async def nsfw_check(client: httpx.AsyncClient, image: str) -> str:
    try:
        response = await client.post(
            "https://aienhancer.ai/api/v1/r/nsfw-detection",
            json={"image": image},
            headers=HEADERS,
        )
        response.raise_for_status()

        task_id = response.json()["data"]["id"]

        for _ in range(20):  # ⛔ max retries (avoid infinite loop)
            await asyncio.sleep(2)

            res = await client.post(
                "https://aienhancer.ai/api/v1/r/nsfw-detection/result",
                json={"task_id": task_id},
                headers=HEADERS,
            )
            res.raise_for_status()

            data = res.json()["data"]

            if data.get("status") == "succeeded":
                return data.get("output")

        raise Exception("NSFW timeout")

    except Exception as e:
        raise Exception(f"NSFW ERROR: {str(e)}")


# -------------------- IMAGE EDIT --------------------

async def image_editor(client: httpx.AsyncClient, image: str, prompt: str) -> dict:
    try:
        settings = encrypt({
            "prompt": prompt,
            "size": "2K",
            "aspect_ratio": "match_input_image",
            "output_format": "jpeg",
            "max_images": 1,
        })

        response = await client.post(
            "https://aienhancer.ai/api/v1/k/image-enhance/create",
            json={
                "model": 2,
                "image": image,
                "function": "ai-image-editor",
                "settings": settings,
            },
            headers=HEADERS,
        )
        response.raise_for_status()

        task_id = response.json()["data"]["id"]

        for _ in range(30):  # ⛔ prevent infinite loop
            await asyncio.sleep(2.5)

            res = await client.post(
                "https://aienhancer.ai/api/v1/k/image-enhance/result",
                json={"task_id": task_id},
                headers=HEADERS,
            )
            res.raise_for_status()

            data = res.json()["data"]

            if data.get("status") == "success" and data.get("output"):
                return {
                    "id": task_id,
                    "output": data["output"],
                    "input": data.get("input"),
                }

            if data.get("status") == "failed":
                raise Exception("AIEnhancer failed processing")

        raise Exception("Image processing timeout")

    except Exception as e:
        raise Exception(f"EDITOR ERROR: {str(e)}")


# -------------------- ROUTES --------------------

@app.get("/")
async def root():
    return {
        "success": True,
        "message": "Use POST /edit-image",
    }


@app.post("/edit-image")
async def edit_image(request: EditRequest):
    # ✅ Validate base64
    try:
        image_bytes = base64.b64decode(request.image, validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 image")

    if not image_bytes:
        raise HTTPException(status_code=400, detail="Empty image")

    image_data_url = f"data:image/jpeg;base64,{request.image}"

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
        # ---------- NSFW ----------
        try:
            nsfw_result = await nsfw_check(client, image_data_url)
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))

        if nsfw_result != "normal":
            raise HTTPException(status_code=400, detail="NSFW blocked")

        # ---------- EDIT ----------
        try:
            result = await image_editor(client, image_data_url, request.prompt)
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))

        return {
            "success": True,
            "job_id": result["id"],
            "image": result["output"],
        }