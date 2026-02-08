import requests
import json

def fetch_agenda(user_id):
    # Date à changer
    url = f"https://reverse-proxy.eseo.fr/API-SP/API/agenda/user/20260126T060000/20260215T210000/{user_id}"
    
    try:
        response = requests.get(url)
        response.raise_for_status()

        content_text = response.content.decode('utf-8-sig').strip()

        data = json.loads(content_text)
        cours_liste = []
        
        # L'API retourne un array JSON avec des événements
        for event in data:
            def get_val(key):
                return event.get(key, "N/A")

            cours = {
                'titre': get_val('Libelle'),
                'debut': get_val('Debut'),
                'salle': get_val('Emplacement'),
                'prof': get_val('Professeur')
            }
            cours_liste.append(cours)
            
        return cours_liste

    except requests.exceptions.RequestException as e:
        return f"Erreur de connexion : {e}"
    except json.JSONDecodeError as e:
        return f"Erreur de lecture JSON (Vérifie l'ID ou l'accès au lien) : {e}"

# mon_id = 54024 # Ton ID trouvé dans la console
# resultat = fetch_agenda(mon_id)

# if isinstance(resultat, list):
#     print(f"{len(resultat)} cours trouvés :")
#     for c in resultat:
#         print(f"[{c['debut']}] {c['titre']} - Salle: {c['salle']}")
# else:
#     print(resultat)