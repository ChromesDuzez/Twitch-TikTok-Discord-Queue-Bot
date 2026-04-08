# Webhook Integration Guide

## Overview

Your bot now has a webhook endpoint that listens for incoming HTTP requests from your Odoo database. When events occur in Odoo (like punch approvals or updates), Odoo can POST to your bot's webhook endpoint to trigger Discord message updates.

## Setup

### 1. Environment Variables

Add these to your `.env` file:

```
WEBHOOK_PORT=8080
ODOO_URL=https://your-odoo-instance.com
ODOO_DB=your_database_name
ODOO_USERNAME=your_username
ODOO_API_KEY=your_api_key
```

- **WEBHOOK_PORT**: The port your bot will listen on for webhook events (default: 8080)
- **ODOO_URL/DB/USERNAME/API_KEY**: Your Odoo configuration for dynamic IP verification

### 2. Security Features

Your webhook now includes:

- **Dynamic Odoo Verification**: On every webhook request, the bot calls your Odoo API to verify connectivity. This validates that the request is coming through the Odoo system.
- **Input Sanitization**: All incoming data is validated and sanitized to prevent injection attacks:
  - String length limits (max 2000 chars)
  - Character whitelisting (alphanumeric, common punctuation, whitespace)
  - Null byte removal
  - Recursive data object validation
  - Type checking
- **IP Logging**: All requests are logged with source IP for audit trails

### 3. Network Configuration

If your bot is running locally or behind a firewall, you'll need to expose the webhook endpoint to Odoo:

- **Local testing**: Use [ngrok](https://ngrok.com/) to create a public tunnel
  ```powershell
  ngrok http 8080
  ```
- **Production**: Use a reverse proxy or expose the port directly (ensure firewall rules allow it)

Your webhook URL will be something like:
```
https://your-domain.com:8080/webhook/timetracking
```

## Webhook Endpoints

### POST `/webhook/timetracking`

Main endpoint for timetracking related events.

**Required Headers:**
```
Content-Type: application/json
```

**Note:** No authorization token is required in the request. Instead, the bot validates all requests by:
1. Making a verification call to your Odoo API (validates you're sending from Odoo)
2. Sanitizing all payload data
3. Checking request source IP for audit logging

**Payload Format:**

```json
{
  "channel_id": 1234567890,
  "message_id": 9876543210,
  "punch_id": 5,
  "action": "update_content",
  "content": "New message content here",
  "data": {}
}
```

## Supported Actions

### 1. `update_content`
Update the text content of a Discord message.

```json
{
  "channel_id": 1234567890,
  "message_id": 9876543210,
  "action": "update_content",
  "content": "Punch approved in Odoo"
}
```

### 2. `approve_punch`
Update the approval status of a punch and modify buttons accordingly.

```json
{
  "channel_id": 1234567890,
  "message_id": 9876543210,
  "punch_id": 5,
  "action": "approve_punch",
  "data": {
    "punch_approval_status": {
      "punchInApproval": true,
      "punchOutApproval": true
    }
  }
}
```

If both `punchInApproval` and `punchOutApproval` are `true`, the approval buttons will be removed.

### 3. `update_clock_view`
Refresh the time clock view for an employee (updates buttons based on current state).

```json
{
  "channel_id": 1234567890,
  "message_id": 9876543210,
  "action": "update_clock_view",
  "data": {
    "employee_id": 123456789,
    "is_clocked_in": true,
    "current_punch": 5
  }
}
```

### 4. `sync_database`
Query the database to get the current punch status and update the message view accordingly.

```json
{
  "channel_id": 1234567890,
  "message_id": 9876543210,
  "punch_id": 5,
  "action": "sync_database"
}
```

## Implementation Examples

### Curl Example

```bash
curl -X POST http://localhost:8080/webhook/timetracking \
  -H "Content-Type: application/json" \
  -d '{
    "channel_id": 1234567890,
    "message_id": 9876543210,
    "punch_id": 5,
    "action": "approve_punch",
    "data": {
      "punch_approval_status": {
        "punchInApproval": true,
        "punchOutApproval": true
      }
    }
  }'
```

### Python Example

```python
import requests
import json

webhook_url = "https://your-domain.com:8080/webhook/timetracking"

payload = {
    "channel_id": 1234567890,
    "message_id": 9876543210,
    "punch_id": 5,
    "action": "approve_punch",
    "data": {
        "punch_approval_status": {
            "punchInApproval": True,
            "punchOutApproval": True
        }
    }
}

headers = {
    "Content-Type": "application/json"
}

response = requests.post(webhook_url, json=payload, headers=headers)
print(response.status_code, response.text)
```

### Odoo Automated Action Example

In Odoo, you can create an automated action that calls your webhook:

1. Go to **Automation > Automated Actions**
2. Create a new action triggered on punch approval
3. Add a server action to POST to your webhook endpoint with the appropriate payload

## Data Flow

```
Odoo Database Event
        ↓
Odoo triggers webhook POST
        ↓
Bot receives request on /webhook/timetracking
        ↓
Verify Content-Type header is application/json
        ↓
Call Odoo API to verify connectivity
(handles dynamic IPs for Odoo Online sharded instances)
        ↓
Parse incoming JSON
        ↓
Validate and sanitize all payload fields
(type checking, string limits, injection prevention)
        ↓
Route to TimeTracking cog
        ↓
handle_odoo_webhook() processes the payload
        ↓
Fetches Discord message by channel_id + message_id
        ↓
Updates message content or view based on action
        ↓
Returns 200 OK
        ↓
Request logged with source IP for audit trail
```

## Error Handling

The webhook endpoint will return:

- **200 OK** - Webhook processed successfully
- **400 Bad Request** - Invalid JSON, missing fields, invalid data types, or sanitization failure
- **403 Forbidden** - Odoo API verification failed (connection could not be established)
- **404 Not Found** - Unknown webhook route (path not `/webhook/timetracking`)
- **500 Internal Server Error** - Unexpected processing error (check bot logs)

Each error response includes a descriptive message explaining what failed.

## Logging

All webhook events are logged to the console with the `[Webhook]` prefix, including request source IP. Check your bot's terminal for:

- `Webhook server started on port 8080` - Server is running and listening
- `Odoo connection verified` - Odoo API verification successful
- `Successfully processed webhook from IP: x.x.x.x` - Request processed successfully
- `Rejected request - Odoo verification failed from IP: x.x.x.x` - Odoo API call failed
- `Rejected request - Invalid JSON from IP: x.x.x.x` - Malformed JSON
- `Rejected request - Validation error from IP x.x.x.x: [error]` - Payload validation failed
- `Rejected request - Unknown route from IP: x.x.x.x` - Wrong endpoint path
- `[Webhook] Unexpected error from IP x.x.x.x: [error]` - Server error occurred

## Input Sanitization Details

Your webhook includes comprehensive input validation and sanitization to prevent injection attacks:

### String Sanitization
- **Max length**: 2000 characters (content), 500 characters (nested data)
- **Null bytes**: Removed to prevent null-byte injection
- **Character whitelist**: Only alphanumeric, spaces, and common punctuation allowed
- **Effect**: Prevents script injection and command injection attacks

### Integer Validation
- **Type checking**: Must be valid integers
- **Range validation**: Must be positive and within Discord's valid ID range
- **Effect**: Prevents integer overflow and out-of-range attacks

### Data Object Validation
- **Recursive validation**: Nested objects are validated at each level
- **Key name validation**: Object keys must follow Python variable naming rules
- **Type enforcement**: Each value must match an allowed type (bool, int, float, str, dict, null)
- **Effect**: Prevents arbitrary object injection and type confusion attacks

### Field Validation
- **Required fields**: `channel_id` and `message_id` are mandatory
- **Optional fields**: `punch_id`, `content`, `data` are optional but validated if present
- **Action enum**: `action` must be one of: `update_content`, `approve_punch`, `update_clock_view`, `sync_database`
- **Effect**: Prevents missing data errors and restricts operations to safe actions

### Examples of Rejected Payloads

```json
// ❌ SQL Injection attempt
{
  "channel_id": 1234567890,
  "message_id": 9876543210,
  "content": "'; DROP TABLE punch_clock; --"
}
```
Rejected: String contains unsupported characters (`;`, `'`, `-`)

```json
// ❌ Script injection attempt
{
  "channel_id": 1234567890,
  "message_id": 9876543210,
  "content": "<script>alert('xss')</script>"
}
```
Rejected: String contains unsupported characters (`<`, `>`, `(`, `)`)

```json
// ❌ Type confusion
{
  "channel_id": "not_a_number",
  "message_id": 9876543210
}
```
Rejected: `channel_id` must be an integer

```json
// ❌ Fields exceed max length
{
  "channel_id": 1234567890,
  "message_id": 9876543210,
  "content": "A very long string that exceeds 2000 character limit..."
}
```
Rejected: String exceeds maximum length

## Security Considerations

Your webhook includes multiple layers of security:

### Dynamic Odoo Verification
- **On every webhook request**, the bot makes a call to your Odoo API to verify connectivity
- This works seamlessly with Odoo Online's sharded infrastructure (handles IP changes automatically)
- If Odoo verification fails, the request is rejected immediately

### Input Sanitization
- **String validation**: Max 2000 characters, null byte removal, character whitelisting
- **Type checking**: All fields validated for correct types (int, string, bool, dict)
- **SQL injection prevention**: Data is never used in SQL queries directly; Discord operations use the validated IDs
- **XSS prevention**: Content strings are sanitized before being sent to Discord
- **Recursive validation**: Nested data objects are validated recursively

### Request Logging
- All requests are logged with source IP for audit trails
- Failed requests log the reason (invalid JSON, missing fields, Odoo verification failed)
- Successful requests log confirmation and source IP

### Best Practices
1. **Always use HTTPS** in production - don't send data over unencrypted HTTP
2. **Monitor logs** - Watch for validation failures or suspicious patterns
3. **Use Odoo API credentials**: Ensure your Odoo API key is secure and restricted to minimal permissions
4. **Rate limiting**: Consider adding rate limiting at your network level
5. **Firewall restrictions**: If possible, restrict webhook connections to Odoo's IP ranges

## Troubleshooting

### Webhook not receiving requests
- Check `WEBHOOK_PORT` in `.env` matches your network configuration
- Verify firewall allows connections to the port
- For local testing, ensure ngrok is running and has the correct URL configured in Odoo

### Odoo verification failed (403 Forbidden)
- Verify your Odoo credentials in `.env` are correct:
  - `ODOO_URL` is accessible and has no typos
  - `ODOO_DB` matches your database name
  - `ODOO_USERNAME` and `ODOO_API_KEY` are valid
- Check the bot's console logs for specific Odoo API error messages
- Ensure your Odoo API key has permissions to call `/res.partner/search_read`

### Validation error (400 Bad Request)
- **Invalid JSON**: Ensure the JSON payload is properly formatted
- **Missing fields**: `channel_id` and `message_id` are required
- **Out of range integers**: IDs must be positive integers
- **String too long**: Content is limited to 2000 characters
- **Unsupported characters**: Strings must contain only alphanumeric, punctuation, and whitespace
- Check the error message in the response for specifics

### Message not found error
- The Discord message has been deleted
- The channel ID or message ID is incorrect
- The bot doesn't have access to that channel

### Content-Type error (400 Bad Request)
- Ensure the `Content-Type` header is set to `application/json`
- Don't send other content types (form data, plain text, etc.)

### Bot not responding
- Check the bot is running: look for `Webhook server started on port 8080` in logs
- Check bot has internet access to receive requests and call Odoo API
- Verify the endpoint URL is being called correctly
- Look for `[Webhook]` log entries to see what's happening

## Future Enhancements

Consider adding webhooks for:
- Work punch updates
- Customer management changes
- Employee status changes
- Report generation notifications
