import datetime
import os
from dotenv import load_dotenv
import requests
from datetime import datetime
import pytz

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

def UseAPI(endpoint,data):
    if OdooLoaded:
        data["context"] = {"lang": "en_US"}
        response = requests.post(
            f"{OdooURL}{endpoint}",
            headers={
                "Authorization": f"Bearer {OdooKEY}",
                "X-Odoo-Database": f"{OdooDB}"
            },
            json=data
        )
        response.raise_for_status()
        return response.json()



def SearchPartnersbyId(customer_id):
    returnedData = UseAPI("/res.partner/search_read",{
            "domain": [ ["id", "=", customer_id] ],
            "fields": [ "id", "company_type", "display_name" ],
            "limit": 2
        })
    if len(returnedData) == 1:
        return returnedData[0]
    elif len(returnedData) > 1:
        raise ValueError(f"Multiple partners found with ID {customer_id}.")
    else:
        print(f"No partner found with ID {customer_id}.")
        return None
    
def SearchPartnersbyName(customer_name):
    returnedData = UseAPI("/res.partner/search_read",{
            "domain": [ ["display_name", "ilike", customer_name] ],
            "fields": [ "id", "company_type", "display_name" ],
            "limit": 10
        })
    return returnedData
        
def CreatePartner(newPartner, blockDuplicate=True):
    if blockDuplicate:
        existingPartners = SearchPartnersbyName(newPartner)
        if existingPartners:
            print(f"Partner with name '{newPartner}' already exists. Aborting creation.")
            return None

    returnedData = UseAPI("/res.partner/name_create",{
            "name": newPartner
        })
    return returnedData

def AttendanceRead(employee_name, limit=20):
    returnedData = UseAPI("/hr.attendance/search_read",{
            "domain": [ ["employee_id.name", "ilike", employee_name] ],
            "fields": [ "id", "display_name", "employee_id", "check_in", "check_out", "worked_hours" ],
            "order": "check_in desc",
            "limit": limit,
        })
    return returnedData

def ClockOut(employee_name, check_out_time=None):
    mostRecentAttendance = AttendanceRead(employee_name, 1)
    if mostRecentAttendance:
        if mostRecentAttendance[0]["check_out"] is not False:
            raise ValueError(f"Employee {employee_name} is not currently clocked in.")
    else:
        raise ValueError(f"No attendance records found for employee: {employee_name}.")
    attendance_id = mostRecentAttendance[0]["id"]
    if not check_out_time:
        local_timezone = pytz.timezone("America/Chicago") # Replace with your server/user timezone
        now_local = datetime.now(local_timezone)
        now_utc = now_local.astimezone(pytz.utc)
        check_out_time = now_utc.strftime("%Y-%m-%d %H:%M:%S")
    returnedData = UseAPI("/hr.attendance/write",{
            "ids": [ attendance_id ],
            "vals": { "check_out": check_out_time }
        })
    return returnedData


# print(SearchPartnersbyId(3))
# print(SearchPartnersbyId(999999)) # Non-existent ID
# print(SearchPartnersbyName("Test Partner from API"))
# print(CreatePartner("Test Partner from API"))
# print(SearchPartnersbyName("Test"))

ClockOut("Zach Wilson")

data = AttendanceRead("Zach Wilson", 1)
for record in data:
    print(f"Shift ID: {record['id']}, Employee: {record['employee_id']}, Shift Description: {record['display_name']}, Check-in: {record['check_in']}, Check-out: {record['check_out']}, Worked Hours: {round(record['worked_hours'] * 4) / 4}")

