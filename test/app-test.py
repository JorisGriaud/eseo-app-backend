from recupIDeseo import extract_eseo_id
from recupEDT3 import fetch_agenda

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import traceback
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = FastAPI()


@app.get("/")
def read_root():
    return {"message": "World"}

@app.get("/users/id")
async def get_eseo_id(email: str, mot_de_passe: str):
    try:
        logger.info(f"Tentative de connexion avec {email}")
        id = await extract_eseo_id(email, mot_de_passe)
        logger.info(f"ID trouvé : {id}")
        return JSONResponse({"ID": id})
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"
        logger.error(error_msg)
        return JSONResponse({"error": error_msg}, status_code=500)

@app.get("/users/edt/{user_id}")
def get_EDT(user_id: int):
    try:
        logger.info(f"Récupération EDT pour user {user_id}")
        edt = fetch_agenda(user_id)
        logger.info(f"EDT récupéré : {edt}")
        return JSONResponse({"EDT": edt})
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}"
        logger.error(error_msg)
        return JSONResponse({"error": error_msg}, status_code=500)