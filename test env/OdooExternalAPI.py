import datetime
import os
from dotenv import load_dotenv
from datetime import datetime, timedelta,date
import requests
import pytz
import json

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
        if response.status_code == 500:
            try:
                error_data = response.json()
                print("Odoo API Error Details:")
                for key, value in error_data.items():
                    print(f"-=-=-=-{key}-=-=-=-")
                    print(f"{value}")
                # You can specifically look for error_data['error']['data']['message'] or the full traceback
            except json.JSONDecodeError:
                print(f"Response not in JSON format. Raw response: {response.text}")
            raise Exception(f"Odoo API request failed with status code {response.status_code}.")
        else:
            response.raise_for_status()
            return response.json()

## Customer/Partner/Employee (Contact) Functions ##

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
    
def SearchPartnersbyName(customer_name, limit=10):
    returnedData = UseAPI("/res.partner/search_read",{
            "domain": [ ["display_name", "ilike", customer_name] ],
            "fields": [ "id", "company_type", "display_name" ],
            "limit": limit
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

def getEmployeeByID(employee_id):
    returnedData = UseAPI("/hr.employee/search_read",{
            "domain": [ ["active", "=", True], ["id", "=", employee_id] ],
            "fields": [ "id", "display_name" ]
        })
    if len(returnedData) == 1:
        return returnedData[0]
    elif len(returnedData) > 1:
        raise ValueError(f"Multiple employees found with ID {employee_id}.")
    else:        
        raise ValueError(f"No employee found with ID {employee_id}.")

def getEmployeeList():
    returnedData = UseAPI("/hr.employee/search_read",{
            "domain": [ ["active", "=", True] ],
            "fields": [ "id", "display_name" ],
            "order": "id asc"
        })
    return returnedData


### Work Time Functions ###

def GetFieldServiceTasksByCustomer(project_id=2, name_filter="a%"):
    returnedData = UseAPI("/project.task/search_read",{
            "domain": [ [ "partner_id.display_name", "ilike", name_filter ], ["project_id", "=", project_id],
                        ["is_closed", "=", False] ],
            "fields": [ "display_name", "partner_id", "project_id", "is_closed", "date_deadline", "date_end" ],
            "limit": 5
        })
    return returnedData

def GetFieldServiceTasksByID(task_id):
    returnedData = UseAPI("/project.task/search_read",{
            "domain": [ ["id", "=", task_id] ],
            "fields": [ "display_name", "partner_id", "project_id", "company_id", "is_closed", "date_deadline", 
                       "date_end", "parent_id" ],
            "limit": 5
        })
    if len(returnedData) == 1:
        return returnedData[0]
    elif len(returnedData) > 1:
        raise ValueError(f"Task with ID {task_id} has multiple entries.")
    else:
        raise ValueError(f"No task found with ID {task_id}.")

def GetProjects():
    returnedData = UseAPI("/project.project/search_read",{
            "domain": [ "|", ["active", "=", True], ["active", "=", False] ],
            "fields": [ "display_name" ],
            "order": "id asc"
        })
    return returnedData

def GetTimeEntriesForTask(task_id):
    returnedData = UseAPI("/project.task/search_read",{
            "domain": [ ["id", "=", task_id] ],
            "fields": [ "timesheet_ids" ]
        })
    if len(returnedData) == 1:
        return returnedData[0]
    elif len(returnedData) > 1:
        raise ValueError(f"Task with ID {task_id} has multiple entries.")
    else:
        raise ValueError(f"No task found with ID {task_id}.")

def GetTimeEntryDetails(timesheet_id):
    returnedData = UseAPI("/account.analytic.line/search_read",{
            "domain": [ ["id", "=", timesheet_id] ],
            "fields": [ "id", "name", "display_name", "employee_id", "parent_task_id", "project_id", 
                        "task_id", "date", "unit_amount", "product_uom_id", "amount", "company_id", "validated_status" ]
        })
    return returnedData

#Required Fields: amount (Monetary), company_id (many2one), date (date), name (char)
def addWorkTimeOnTask(task_id, date, employee_id, description, quantity: float=None, amount=0):
    task = GetFieldServiceTasksByID(task_id)
    returnedData = UseAPI("/account.analytic.line/create",{
            "vals_list": [ {
                "name": description, # Description of the timesheet entry
                "date": date, # Date of the work (can be a string in 'YYYY-MM-DD' format)
                "unit_amount": quantity, # Hours worked
                "product_uom_id": 4, # Hours
                "amount": amount, # Cost amount for the timesheet entry (can be 0 if not tracking costs)
                "employee_id": employee_id, # Many2one to hr.employee
                "company_id": task["company_id"], # Many2one to res.company (Swim Shack, Inc. is Default probably can tie it to the company on the task/project)
                "validated_status": "draft", # Set to 'validated' if you want to auto-validate the timesheet entry
                "task_id": task_id, # Many2one to project.task
                "project_id": task["project_id"][0], # Many2one to project.project (Field Service)
                "parent_task_id": task["parent_id"] # Many2one to project.task (Parent Task if this is a subtask)
            }]
        })
    return returnedData


### Attendance Functions ###

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

def mostRecentClock(employee_id):
    returnedData = UseAPI("/hr.attendance/search_read",{
            "domain": [ ["employee_id", "=", employee_id] ],
            "fields": [ "id", "date", "check_in", "check_out" ],
            "order": "check_in desc",
            "limit": 1
        })
    return returnedData

def getCurrentClockedStatus(employee_id):
    if mostRecentClock(employee_id)[0]["check_out"] is False:
        print(f"{getEmployeeByID(employee_id)['display_name']} is currently clocked in.")
        return "in"
    else:
        print(f"{getEmployeeByID(employee_id)['display_name']} is currently clocked out.")
        return "out"


# print(SearchPartnersbyId(3))
# print(SearchPartnersbyId(999999)) # Non-existent ID
# print(SearchPartnersbyName("Test Partner from API"))
# print(CreatePartner("Test Partner from API"))
# print(SearchPartnersbyName("Test"))

#ClockOut("Zach Wilson")


print("-=-=-=-=-Employees-=-=-=-=-=-")
data = getEmployeeList()
print(f"Total Employees: {len(data)}")
for record in data:
    print(record)

data = AttendanceRead("Zach Wilson", 1)
for record in data:
    print(f"Shift ID: {record['id']}, Employee: {record['employee_id']}, Shift Description: {record['display_name']}, Check-in: {record['check_in']}, Check-out: {record['check_out']}, Worked Hours: {round(record['worked_hours'] * 4) / 4}")

print(SearchPartnersbyName("Jason & Jen Morris"))

print("-=-=-=-=-Projects-=-=-=-=-=-")
data = GetProjects()
print(len(data))
for record in data:
    print(record)

print("-=-=-=-=-Project Tasks-=-=-=-=-=-")
data = GetFieldServiceTasksByCustomer(2, "Morris")
for record in data:
    print(record)

#task id: 2988
print("-=-=-=-=-Project Tasks Timesheets-=-=-=-=-=-")
task = 2988
data = GetTimeEntriesForTask(task)
print(f"Available timesheets for task {task}: {len(data['timesheet_ids'])} \nData: {data}")
for record in data["timesheet_ids"]:
    print(GetTimeEntryDetails(record))
print("-=-Adding Time Entry-=-")
print("It works! Check Odoo to see the new timesheet entry. Remember it will be in draft status so it won't affect reports until validated.")
#print(addWorkTimeOnTask(task, "2026-03-24", 1.00, "Test API Entry", quantity=1))


print("-=-Clock In/Out-=-")
status = getCurrentClockedStatus(1) # Zach Wilson