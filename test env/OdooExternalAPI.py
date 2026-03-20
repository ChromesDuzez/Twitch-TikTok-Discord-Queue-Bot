import os
from dotenv import load_dotenv
import requests

load_dotenv() 

OdooLoaded = False
OdooURL = os.getenv("ODOO_URL", None)
OdooDB = os.getenv("ODOO_DB", None)
OdooUSERNAME = os.getenv("ODOO_USERNAME", None)
OdooKEY = os.getenv("ODOO_API_KEY", None)

if not all([OdooURL, OdooDB, OdooUSERNAME, OdooKEY]):
    print("Odoo configuration is incomplete. Please check your .env file if you wish to use the Odoo integration.")
else:
    OdooLoaded = True
    print("Odoo configuration loaded successfully.")


## Example of making a request to Odoo API to fetch partners whose display name starts with 'a'
if OdooLoaded:
    response = requests.post(
        f"{OdooURL}/res.partner/search_read",
        headers={
            "Authorization": f"Bearer {OdooKEY}",
            "X-Odoo-Database": f"{OdooDB}"
        },
        json={
            "context": {"lang": "en_US"},
            "domain": [
                [
                    "id",
                    "=",
                    1
                ]
            ],
            "fields": [
                "id",
                "display_name"
            ],
            "limit": 10
        },
    )
    response.raise_for_status()
    data = response.json()
    print(data)