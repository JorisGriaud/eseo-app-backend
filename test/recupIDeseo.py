import asyncio
from playwright.sync_api import sync_playwright

def extract_eseo_id_sync(email, password):
    """Version synchrone pour FastAPI"""
    with sync_playwright() as p:
        # On lance le navigateur
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        try:
            # 1. Connexion au portail
            page.goto("https://reseaueseo.sharepoint.com/sites/etu/Pages/Mon-emploi-du-temps.aspx")
            
            # On remplit les champs Microsoft (si la session n'est pas déjà sauvée)
            page.fill('input[type="email"]', email)
            page.click('input[type="submit"]')
            page.wait_for_selector('input[type="password"]')
            page.fill('input[type="password"]', password)
            page.click('input[type="submit"]')
            
            # On attend d'être sur la page finale
            page.wait_for_selector('#calendar')

            # 2. On récupère la variable 'idUser' directement dans le JS de la page
            id_user = page.evaluate("() => window.idUser")
            
            # Si la variable n'est pas encore prête, on attend un peu
            attempts = 0
            while (id_user == "00000" or id_user is None) and attempts < 10:
                import time
                time.sleep(1)
                id_user = page.evaluate("() => window.idUser")
                attempts += 1

            return id_user
        finally:
            browser.close()

async def extract_eseo_id(email, password):
    """Wrapper async pour FastAPI"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, extract_eseo_id_sync, email, password)

# Optionnel pour tests directs
# id = extract_eseo_id_sync("email@test.com", "password")
# print(f"ID trouvé : {id}")