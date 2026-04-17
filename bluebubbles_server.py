#!/usr/bin/env python3
import asyncio
import os
import sys
import logging
import functools
import httpx
import uuid
from datetime import datetime, timezone
from mcp.server.fastmcp import FastMCP
from dateutil import parser

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger("bluebubbles-server")

mcp = FastMCP("bluebubbles")

BLUEBUBBLES_PASSWORD = os.environ.get("BLUEBUBBLES_PASSWORD", "")
TIMEOUT = 30

def _build_base_url() -> str:
    url = os.environ.get("BLUEBUBBLES_URL", "").rstrip('/')
    if url and not url.startswith('http'):
        url = f"http://{url}"
    return url

BASE_URL = _build_base_url()
_client = httpx.AsyncClient()


def parse_limit(limit: str, default: int, max_val: int) -> int:
    try:
        return min(int(limit) if limit.strip() else default, max_val)
    except ValueError:
        return default


def handle_tool_errors(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            action = fn.__name__.replace('_', ' ')
            logger.error(f"Error in {action}: {e}")
            return f"❌ Error in {action}: {str(e)}"
    return wrapper


def require_field(value: str, name: str) -> str | None:
    if not value.strip():
        return f"❌ Error: {name} is required"
    return None


def check_response(response: dict, success_msg: str) -> str:
    if response.get('status') == 200:
        return f"✅ {success_msg}"
    return f"❌ Failed: {response.get('message', 'Unknown error')}"


async def make_api_request(endpoint: str, method: str = "GET", data: dict = None, params: dict = None):
    if not BASE_URL:
        raise ValueError("BlueBubbles URL not configured")

    url = f"{BASE_URL}/api/v1/{endpoint.lstrip('/')}"
    query_params = {"password": BLUEBUBBLES_PASSWORD}
    if params:
        query_params.update(params)

    if method == "GET":
        response = await _client.get(url, params=query_params, timeout=TIMEOUT)
    elif method == "POST":
        response = await _client.post(url, params=query_params, json=data, timeout=TIMEOUT)
    else:
        raise ValueError(f"Unsupported HTTP method: {method}")

    response.raise_for_status()
    return response.json()


def format_message(msg):
    text = (msg.get('text') or '').strip() or "[No text content]"
    sender = "Me" if msg.get('isFromMe', False) else (msg.get('handle') or {}).get('address', 'Unknown')
    date = msg.get('dateCreated', '')
    try:
        if isinstance(date, (int, float)):
            date_str = datetime.fromtimestamp(date / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
        else:
            date_str = parser.parse(date).strftime('%Y-%m-%d %H:%M')
    except Exception:
        date_str = str(date)
    return f"[{date_str}] {sender}: {text}"


def format_chat(chat):
    label = chat.get('displayName') or chat.get('chatIdentifier') or "Chat"
    participant_count = len(chat.get('participants', []))
    return f"{label} ({participant_count} participants)"


@mcp.tool()
@handle_tool_errors
async def search_messages(query: str = "", chat_id: str = "", limit: str = "20") -> str:
    """Search for messages containing specific text across all chats or in a specific chat."""
    logger.info(f"Searching messages with query: {query}, chat_id: {chat_id}, limit: {limit}")

    if not query.strip():
        return "❌ Error: Query text is required"

    limit_int = parse_limit(limit, 20, 1000)
    body = {
        "limit": limit_int,
        "where": [{"statement": f"message.text LIKE '%{query}%'", "args": []}],
    }
    if chat_id.strip():
        body["chatGuid"] = chat_id

    data = await make_api_request("message/query", "POST", body)
    messages = data.get('data', [])

    if not messages:
        return f"🔍 No messages found containing '{query}'"

    lines = [f"🔍 Found {len(messages)} message(s) containing '{query}':\n"]
    lines.extend(format_message(msg) for msg in messages)
    return "\n".join(lines)


@mcp.tool()
@handle_tool_errors
async def get_recent_messages(chat_id: str = "", limit: str = "10") -> str:
    """Get recent messages from all chats or a specific chat."""
    logger.info(f"Getting recent messages for chat_id: {chat_id}, limit: {limit}")

    limit_int = parse_limit(limit, 10, 50)

    if chat_id.strip():
        data = await make_api_request(f"chat/{chat_id}/message", params={"limit": limit_int})
    else:
        data = await make_api_request("message/query", "POST", {"limit": limit_int})

    messages = data.get('data', [])

    if not messages:
        return "📊 No recent messages found"

    lines = [f"📊 Recent messages ({len(messages)} shown):\n"]
    lines.extend(format_message(msg) for msg in messages)
    return "\n".join(lines)


@mcp.tool()
@handle_tool_errors
async def list_chats(limit: str = "20") -> str:
    """List all available chats/conversations."""
    logger.info(f"Listing chats with limit: {limit}")

    limit_int = parse_limit(limit, 20, 100)
    data = await make_api_request("chat/query", "POST", {"limit": limit_int})
    chats = data.get('data', [])

    if not chats:
        return "📁 No chats found"

    lines = [f"📁 Available chats ({len(chats)} shown):\n"]
    for i, chat in enumerate(chats, 1):
        lines.append(f"{i}. {format_chat(chat)}\n   ID: {chat.get('guid', '')}")
    return "\n".join(lines)


@mcp.tool()
@handle_tool_errors
async def send_message(chat_id: str = "", message: str = "") -> str:
    """Send a message to a specific chat."""
    logger.info(f"Sending message to chat_id: {chat_id}")

    if err := require_field(chat_id, "Chat ID"):
        return err
    if err := require_field(message, "Message text"):
        return err

    data = {"chatGuid": chat_id, "tempGuid": str(uuid.uuid4()), "message": message}
    response = await make_api_request("message/text", "POST", data)
    return check_response(response, f"Message sent successfully to {chat_id}")


@mcp.tool()
@handle_tool_errors
async def send_message_to_number(phone_number: str = "", message: str = "") -> str:
    """Send a message directly to a phone number or email address."""
    logger.info(f"Sending message to number: {phone_number}")

    if err := require_field(phone_number, "Phone number or email"):
        return err
    if err := require_field(message, "Message text"):
        return err

    data = {"addresses": [phone_number], "message": message, "tempGuid": str(uuid.uuid4())}
    response = await make_api_request("chat/new", "POST", data)
    return check_response(response, f"Message sent successfully to {phone_number}")


@mcp.tool()
@handle_tool_errors
async def get_contacts(limit: str = "50") -> str:
    """Get the list of contacts from BlueBubbles."""
    logger.info(f"Getting contacts with limit: {limit}")

    limit_int = parse_limit(limit, 50, 200)
    data = await make_api_request("contact", params={"limit": limit_int})
    contacts = data.get('data', [])

    if not contacts:
        return "📊 No contacts found"

    lines = [f"📊 Contacts ({len(contacts)} shown):\n"]
    for contact in contacts:
        name = " ".join(filter(None, [contact.get('firstName', ''), contact.get('lastName', '')])) or "Unknown"
        lines.append(f"• {name}")
        for phone in contact.get('phoneNumbers', []):
            lines.append(f"  📱 {phone.get('address', '')}")
        for email in contact.get('emails', []):
            lines.append(f"  📧 {email.get('address', '')}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
@handle_tool_errors
async def mark_chat_read(chat_id: str = "") -> str:
    """Mark all messages in a chat as read."""
    logger.info(f"Marking chat as read: {chat_id}")

    if err := require_field(chat_id, "Chat ID"):
        return err

    response = await make_api_request(f"chat/{chat_id}/read", "POST", {})
    return check_response(response, f"Chat {chat_id} marked as read")


@mcp.tool()
@handle_tool_errors
async def get_server_info() -> str:
    """Get information about the BlueBubbles server."""
    logger.info("Getting server info")

    data = await make_api_request("server/info")
    info = data.get('data', {})
    lines = [
        "🌐 BlueBubbles Server Information:\n",
        f"• OS Version: {info.get('os_version', 'Unknown')}",
        f"• Server Version: {info.get('server_version', 'Unknown')}",
        f"• Private API: {'Enabled' if info.get('private_api', False) else 'Disabled'}",
        f"• Proxy Service: {info.get('proxy_service', 'Unknown')}",
    ]
    return "\n".join(lines)


@mcp.tool()
@handle_tool_errors
async def get_chat_details(chat_id: str = "") -> str:
    """Get detailed information about a specific chat."""
    logger.info(f"Getting details for chat: {chat_id}")

    if err := require_field(chat_id, "Chat ID"):
        return err

    data = await make_api_request(f"chat/{chat_id}")
    chat = data.get('data')

    if not chat:
        return f"❌ Chat {chat_id} not found"

    lines = [
        "📁 Chat Details:\n",
        f"• Display Name: {chat.get('displayName', 'N/A')}",
        f"• Chat ID: {chat.get('guid', '')}",
        f"• Is Group: {'Yes' if chat.get('isGroup', False) else 'No'}",
    ]
    participants = chat.get('participants', [])
    if participants:
        lines.append(f"\n👥 Participants ({len(participants)}):")
        for p in participants:
            lines.append(f"  • {p.get('address', 'Unknown')}")
    return "\n".join(lines)


if __name__ == "__main__":
    logger.info("Starting BlueBubbles MCP server...")

    if not BASE_URL:
        logger.warning("BLUEBUBBLES_URL not set - server will need configuration")
    if not BLUEBUBBLES_PASSWORD:
        logger.warning("BLUEBUBBLES_PASSWORD not set - authentication may fail")

    try:
        mcp.run(transport='stdio')
    except Exception as e:
        logger.error(f"Server error: {e}", exc_info=True)
        sys.exit(1)
    finally:
        asyncio.run(_client.aclose())
