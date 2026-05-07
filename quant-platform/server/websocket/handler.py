# server/websocket/handler.py
from datetime import datetime
from fastapi import WebSocket, WebSocketDisconnect
from server.websocket.manager import manager
from server.market.broadcaster import MarketBroadcaster

market_broadcaster = MarketBroadcaster()

async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            data = await ws.receive_json()
            await handle_client_message(data, ws)
    except WebSocketDisconnect:
        await manager.disconnect(ws)
    except Exception as e:
        print(f"WebSocket 连接错误: {e}")
        await manager.disconnect(ws)

async def handle_client_message(data: dict, ws: WebSocket):
    message_type = data.get('type')
    message_data = data.get('data', {})

    match message_type:
        case 'subscribe':
            await handle_subscribe_message(message_data, ws)
        case 'unsubscribe':
            await handle_unsubscribe_message(message_data, ws)
        case 'ping':
            await handle_ping_message(ws)
        case _:
            print(f"未知消息类型: {message_type}")

async def handle_subscribe_message(data: dict, ws: WebSocket):
    codes = data.get('codes', [])
    await market_broadcaster.subscribe_client(ws, codes)
    await ws.send_json({
        'type': 'subscribed',
        'data': {'codes': codes}
    })

async def handle_unsubscribe_message(data: dict, ws: WebSocket):
    codes = data.get('codes', [])
    await market_broadcaster.unsubscribe_client(ws, codes)
    await ws.send_json({
        'type': 'unsubscribed',
        'data': {'codes': codes}
    })

async def handle_ping_message(ws: WebSocket):
    await ws.send_json({
        'type': 'pong',
        'data': {'timestamp': datetime.now().isoformat()}
    })