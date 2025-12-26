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
    print("🚀 Loading WithoutBG model...")
    try:
        bg_remover = WithoutBG()  # ⚠️ NO ARGUMENTS
        print("✅ WithoutBG loaded successfully")
    except Exception as e:
        print(f"❌ Failed to load WithoutBG: {e}")
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

    if res.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to fetch credits")

    data = res.json()
    if not data:
        raise HTTPException(status_code=404, detail="User not found")

    return data[0]["rembg_credits"]

async def deduct_credit(user_email: str) -> int:
    current_credits = await get_user_credits(user_email)

    if current_credits <= 0:
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

    if res.status_code not in [200, 204]:
        raise HTTPException(status_code=500, detail="Failed to deduct credit")

    return new_credits

async def refund_credit(user_email: str):
    try:
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
    except Exception as e:
        print(f"⚠️ Refund failed: {e}")

# ===================
# ROUTES
# ===================
@app.get("/")
async def root():
    return {"status": "FastAPI WithoutBG service running"}

@app.post("/")
async def remove_background(
    request_data: RequestData,
    authorization: str = Header(None)
):
    # -------------------
    # AUTH
    # -------------------
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid Authorization header")

    token = authorization.split(" ")[1]

    try:
        payload = jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            options={"verify_aud": False}
        )
        user_email = payload.get("email")
        if not user_email:
            raise HTTPException(status_code=401, detail="Email missing in token")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")

    # -------------------
    # CREDITS
    # -------------------
    try:
        remaining_credits = await deduct_credit(user_email)
    except Exception:
        raise

    # -------------------
    # IMAGE DECODE
    # -------------------
    try:
        if "," in request_data.data_sent:
            img_data = base64.b64decode(request_data.data_sent.split(",")[1])
        else:
            img_data = base64.b64decode(request_data.data_sent)

        input_image = Image.open(BytesIO(img_data)).convert("RGB")
    except Exception:
        await refund_credit(user_email)
        raise HTTPException(status_code=400, detail="Invalid image data")

    # -------------------
    # BACKGROUND REMOVAL
    # -------------------
    try:
        output_image = bg_remover(input_image)

        buffer = BytesIO()
        output_image.save(buffer, format="PNG", optimize=True)
        buffer.seek(0)

        encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")

        return {
            "data_received": f"data:image/png;base64,{encoded}",
            "remaining_credits": remaining_credits
        }
    except Exception as e:
        await refund_credit(user_email)
        raise HTTPException(status_code=500, detail=f"Processing failed: {e}")
