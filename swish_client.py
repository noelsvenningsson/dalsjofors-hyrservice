from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Optional, Dict, Any


@dataclass
class SwishConfig:
    base_url: str  # MSS/test/prod
    merchant_alias: str
    callback_url: str
    mock: bool = True  # True tills du har cert


class SwishClient:
    def __init__(self, cfg: SwishConfig):
        self.cfg = cfg

    def create_payment_request(
        self,
        amount_sek: int,
        message: str,
        callback_url_placeholder: Optional[str] = None,
        payer_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Returns dict with:
          instruction_uuid, token, request_id, swish_app_url
        MOCK: genererar fejk-token så frontend/DB-flöde kan byggas klart.
        """
        instruction_uuid = str(uuid.uuid4())
        token = str(uuid.uuid4()).replace("-", "")
        request_id = instruction_uuid

        callback_url = callback_url_placeholder or self.cfg.callback_url
        swish_app_url = f"swish://paymentrequest?token={token}&callbackurl={callback_url}"

        return {
            "instruction_uuid": instruction_uuid,
            "token": token,
            "request_id": request_id,
            "swish_app_url": swish_app_url,
        }

    def get_payment_request(self, request_id: str) -> Dict[str, Any]:
        """
        MOCK: alltid PENDING.
        """
        return {"id": request_id, "status": "PENDING"}

    def get_qr_svg(self, token: str) -> str:
        """
        Returnerar SVG-sträng. (Du har redan QR-endpoint, så detta kan kopplas där senare.)
        """
        # Placeholder: frontend kan använda befintlig QR-route.
        return f"<svg xmlns='http://www.w3.org/2000/svg' width='200' height='200'><text x='10' y='100'>token:{token}</text></svg>"
