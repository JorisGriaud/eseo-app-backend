import asyncio
from playwright.async_api import async_playwright

async def recuperer_emploi_du_temps():
    async with async_playwright() as p:
        # Lancement du navigateur (headless=False pour voir ce qu'il fait pendant tes tests)
        browser = await p.chromium.launch(headless=True)
        
        # On crée un contexte (comme une fenêtre de navigation privée)
        context = await browser.new_context()
        page = await context.new_page()

        print("Navigation vers le portail...")
        await page.goto("https://my.eseo.fr")

        # 1. Remplir l'email Microsoft
        # Playwright attend automatiquement que le champ apparaisse
        await page.fill('input[type="email"]', "")
        await page.click('input[type="submit"]') # Bouton "Suivant"

        # 2. Remplir le mot de passe
        # On attend un peu que la transition se fasse
        await page.wait_for_selector('input[type="password"]')
        await page.fill('input[type="password"]', "")
        await page.click('input[type="submit"]') # Bouton "Connexion"

        # 3. Gérer la question "Rester connecté ?" (très courant chez Microsoft)
        try:
            # On attend le bouton "Oui" pendant max 5 secondes
            await page.wait_for_selector('#idSIButton9', timeout=5000)
            await page.click('#idSIButton9')
        except:
            print("Pas de demande de maintien de connexion.")

        # 4. Arrivée sur l'emploi du temps
        # Ici, tu mets l'URL directe de la page de ton planning
        await page.goto("https://reseaueseo.sharepoint.com/sites/etu/Pages/Mon-emploi-du-temps.aspx")
        await page.wait_for_load_state("networkidle")

        # 5. Extraction des données (Exemple : récupérer tous les titres de cours)
        # Il faudra adapter le sélecteur CSS (.cours-title) à ton site
        cours = await page.query_selector_all(".fc-content")
        
        for c in cours:
            print(await c.inner_text())

        await browser.close()

# Lancer le script
asyncio.run(recuperer_emploi_du_temps())