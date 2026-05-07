# server/subscriptions/manager.py
from typing import List, Dict, Set
from fastapi import WebSocket

class SubscriptionManager:
    def __init__(self):
        self.client_subscriptions: Dict[WebSocket, Set[str]] = {}
        self.code_clients: Dict[str, Set[WebSocket]] = {}

    def add_client(self, ws: WebSocket, codes: List[str]):
        if ws not in self.client_subscriptions:
            self.client_subscriptions[ws] = set()

        for code in codes:
            self.client_subscriptions[ws].add(code)

            if code not in self.code_clients:
                self.code_clients[code] = set()
            self.code_clients[code].add(ws)

    def remove_client(self, ws: WebSocket, codes: List[str]):
        if ws not in self.client_subscriptions:
            return

        for code in codes:
            if code in self.client_subscriptions[ws]:
                self.client_subscriptions[ws].remove(code)

                if code in self.code_clients:
                    if ws in self.code_clients[code]:
                        self.code_clients[code].remove(ws)
                    if not self.code_clients[code]:
                        del self.code_clients[code]

        if not self.client_subscriptions[ws]:
            del self.client_subscriptions[ws]

    def remove_ws(self, ws: WebSocket):
        if ws in self.client_subscriptions:
            codes = list(self.client_subscriptions[ws])
            for code in codes:
                if code in self.code_clients and ws in self.code_clients[code]:
                    self.code_clients[code].remove(ws)
                    if not self.code_clients[code]:
                        del self.code_clients[code]
            del self.client_subscriptions[ws]

    def get_client_codes(self, ws: WebSocket) -> List[str]:
        return list(self.client_subscriptions.get(ws, []))

    def get_code_clients(self, code: str) -> List[WebSocket]:
        return list(self.code_clients.get(code, []))

    def get_all_codes(self) -> Set[str]:
        return set(self.code_clients.keys())

    def get_connected_clients(self) -> int:
        return len(self.client_subscriptions)