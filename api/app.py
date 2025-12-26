from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import base64
from io import BytesIO
from PIL import Image
import httpx
import jwt
from withoutbg import WithoutBG
import traceback
import sys

# ===================
# ENVIRONMENT VARIABLES
# ===================
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")

if not all([SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_JWT_SECRET]):
    raise RuntimeError("Missing SUPABASE environment variables")

# ===================
# FASTAPI APP
# ===================
app = FastAPI()

origins = [
    "http://127.0.0.1:5503",
    "http://localhost:5503",
    "*"  # restrict in production
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===================
# WITHOUTBG MODEL
# ===================
bg_remover = None

@app.on_event("startup")
async def startup_event():
    global bg_remover
    print("🚀 Loading WithoutBG model...", flush=True)
    try:
        bg_remover = WithoutBG()
        print("✅ WithoutBG loaded successfully", flush=True)
    except Exception as e:
        print(f"❌ Failed to load WithoutBG: {e}", flush=True)
        traceback.print_exc()
        raise

# ===================
# REQUEST MODEL
# ===================
class RequestData(BaseModel):
    data_sent: str

# ===================
# SUPABASE CREDIT HELPERS
# ===================
async def get_user_credits(user_email: str) -> int:
    from urllib.parse import quote
    safe_email = quote(user_email)

    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"{SUPABASE_URL}/rest/v1/wondr_users?select=rembg_credits&email=eq.{safe_email}",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Accept": "application/json",
                "Accept-Profile": "wondr_users",
            },
        )

    print(f"📊 GET credits status: {res.status_code}", flush=True)
    
    if res.status_code != 200:
        print(f"❌ Failed to fetch credits: {res.text}", flush=True)
        raise HTTPException(status_code=500, detail="Failed to fetch credits")

    data = res.json()
    if not data:
        print(f"❌ User not found: {user_email}", flush=True)
        raise HTTPException(status_code=404, detail="User not found")

    print(f"✅ Current credits for {user_email}: {data[0]['rembg_credits']}", flush=True)
    return data[0]["rembg_credits"]

async def deduct_credit(user_email: str) -> int:
    current_credits = await get_user_credits(user_email)

    if current_credits <= 0:
        print(f"❌ Insufficient credits for {user_email}", flush=True)
        raise HTTPException(status_code=403, detail="Insufficient credits")

    new_credits = current_credits - 1

    from urllib.parse import quote
    safe_email = quote(user_email)

    async with httpx.AsyncClient() as client:
        res = await client.patch(
            f"{SUPABASE_URL}/rest/v1/wondr_users?email=eq.{safe_email}",
            headers={
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
                "Accept-Profile": "wondr_users",
                "Content-Profile": "wondr_users",
            },
            json={"rembg_credits": new_credits}
        )

    print(f"📊 PATCH credits status: {res.status_code}", flush=True)
    
    if res.status_code not in [200, 204]:
        print(f"❌ Failed to deduct credit: {res.text}", flush=True)
        raise HTTPException(status_code=500, detail="Failed to deduct credit")

    print(f"✅ Credits deducted. New balance: {new_credits}", flush=True)
    return new_credits

async def refund_credit(user_email: str):
    try:
        print(f"🔄 Refunding credit to {user_email}...", flush=True)
        current_credits = await get_user_credits(user_email)

        from urllib.parse import quote
        safe_email = quote(user_email)

        async with httpx.AsyncClient() as client:
            await client.patch(
                f"{SUPABASE_URL}/rest/v1/wondr_users?email=eq.{safe_email}",
                headers={
                    "apikey": SUPABASE_SERVICE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                    "Content-Type": "application/json",
                    "Accept-Profile": "wondr_users",
                    "Content-Profile": "wondr_users",
                },
                json={"rembg_credits": current_credits + 1}
            )
        print(f"✅ Credit refunded to {user_email}", flush=True)
    except Exception as e:
        print(f"⚠️ Refund failed: {e}", flush=True)
        traceback.print_exc()

# ===================
# ROUTES
# ===================
@app.get("/")
async def root():
    model_status = "loaded" if bg_remover else "not loaded"
    return {
        "status": "FastAPI WithoutBG service running",
        "model_status": model_status
    }

@app.post("/")
async def remove_background(
    request_data: RequestData,
    authorization: str = Header(None)
):
    print("=" * 50, flush=True)
    print("🎯 NEW REQUEST RECEIVED", flush=True)
    print("=" * 50, flush=True)
    
    # -------------------
    # AUTH
    # -------------------
    print("🔐 Checking authorization...", flush=True)
    if not authorization or not authorization.startswith("Bearer "):
        print("❌ Invalid authorization header", flush=True)
        raise HTTPException(status_code=401, detail="Invalid Authorization header")

    token = authorization.split(" ")[1]
    print(f"🔑 Token received (first 20 chars): {token[:20]}...", flush=True)

    try:
        payload = jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            options={"verify_aud": False}
        )
        user_email = payload.get("email")
        if not user_email:
            print("❌ Email missing in token", flush=True)
            raise HTTPException(status_code=401, detail="Email missing in token")
        print(f"✅ User authenticated: {user_email}", flush=True)
    except jwt.ExpiredSignatureError:
        print("❌ Token expired", flush=True)
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError as e:
        print(f"❌ Invalid token: {e}", flush=True)
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")

    # -------------------
    # CREDITS
    # -------------------
    print("💳 Checking and deducting credits...", flush=True)
    try:
        remaining_credits = await deduct_credit(user_email)
        print(f"✅ Credits deducted. Remaining: {remaining_credits}", flush=True)
    except HTTPException as e:
        print(f"❌ Credit deduction failed: {e.detail}", flush=True)
        raise
    except Exception as e:
        print(f"❌ Unexpected error during credit deduction: {e}", flush=True)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Credit processing failed")

    # -------------------
    # IMAGE DECODE
    # -------------------
    print("🖼️  Decoding image...", flush=True)
    try:
        if "," in request_data.data_sent:
            img_data = base64.b64decode(request_data.data_sent.split(",")[1])
        else:
            img_data = base64.b64decode(request_data.data_sent)

        print(f"📦 Image data size: {len(img_data)} bytes", flush=True)
        input_image = Image.open(BytesIO(img_data))
        print(f"📐 Image size: {input_image.size}, mode: {input_image.mode}", flush=True)
        
        if input_image.mode != "RGB":
            print(f"🔄 Converting from {input_image.mode} to RGB", flush=True)
            input_image = input_image.convert("RGB")
        
        print("✅ Image decoded successfully", flush=True)
    except Exception as e:
        print(f"❌ Image decode failed: {e}", flush=True)
        traceback.print_exc()
        await refund_credit(user_email)
        raise HTTPException(status_code=400, detail=f"Invalid image data: {str(e)}")

    # -------------------
    # BACKGROUND REMOVAL
    # -------------------
    print("✂️  Removing background...", flush=True)
    try:
        if bg_remover is None:
            print("❌ WithoutBG model not loaded!", flush=True)
            await refund_credit(user_email)
            raise HTTPException(status_code=500, detail="Model not loaded")
        
        print("🔄 Processing with WithoutBG...", flush=True)
        output_image = bg_remover(input_image)
        print(f"✅ Background removed. Output size: {output_image.size}", flush=True)

        print("📦 Encoding to PNG...", flush=True)
        buffer = BytesIO()
        output_image.save(buffer, format="PNG", optimize=True)
        buffer.seek(0)
        
        output_size = len(buffer.getvalue())
        print(f"📊 Output PNG size: {output_size} bytes", flush=True)

        encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
        print(f"✅ Base64 encoded: {len(encoded)} characters", flush=True)

        print("🎉 SUCCESS! Returning result", flush=True)
        return {
            "data_received": f"data:image/png;base64,{encoded}",
            "remaining_credits": remaining_credits
        }
    except Exception as e:
        print(f"❌ Background removal failed: {e}", flush=True)
        print(f"❌ Error type: {type(e).__name__}", flush=True)
        traceback.print_exc()
        await refund_credit(user_email)
        raise HTTPException(status_code=500, detail=f"Processing failed: {str(e)}")
