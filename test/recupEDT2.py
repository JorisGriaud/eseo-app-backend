import asyncio
from playwright.async_api import async_playwright

async def recuperer_json_planning():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False) # True une fois que ça marche
        context = await browser.new_context()
        page = await context.new_page()

        # 1. Connexion Microsoft (ton code précédent)
        # await page.goto("https://my.eseo.fr")
        
        # # 1. Remplir l'email Microsoft
        # # Playwright attend automatiquement que le champ apparaisse
        # await page.fill('input[type="email"]', "")
        # await page.click('input[type="submit"]') # Bouton "Suivant"

        # # 2. Remplir le mot de passe
        # # On attend un peu que la transition se fasse
        # await page.wait_for_selector('input[type="password"]')
        # await page.fill('input[type="password"]', "")
        # await page.click('input[type="submit"]') # Bouton "Connexion"

        # # 3. Gérer la question "Rester connecté ?" (très courant chez Microsoft)
        # try:
        #     # On attend le bouton "Oui" pendant max 5 secondes
        #     await page.wait_for_selector('#idSIButton9', timeout=5000)
        #     await page.click('#idSIButton9')
        # except:
        #     print("Pas de demande de maintien de connexion.")

        # 4. Une fois sur la page de l'emploi du temps
        # On construit les dates au format attendu par ton école (ex: 20260202T060000)
        date_debut = "20260202T060000" 
        date_fin = "20260208T210000"
        
        # On doit récupérer ton 'idUser' (le script JS du site le fait via un appel API)
        # Mais tu peux aussi le trouver une fois pour toute dans ton inspecteur !
        id_user = "00000" 

        api_url = f"https://reverse-proxy.eseo.fr/API-SP/API/agenda/user/{date_debut}/{date_fin}/{id_user}"

        # 5. On demande à Playwright de récupérer le contenu de cette URL
        # L'avantage : il utilise les cookies de ta session déjà connectée !
        response = await page.goto(api_url)
        # planning_json = await response.json()

        # for cours in planning_json:
        #     print(f"Cours: {cours['Libelle']}")
        #     print(f"Salle: {cours['Emplacement']}")
        #     print(f"Prof: {cours['Professeur']}")
        #     print("-" * 20)

        await browser.close()

asyncio.run(recuperer_json_planning())
