from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests


@dataclass
class SwishConfig:
    base_url: str
    merchant_alias: str
    callback_url: str
    cert_path: Optional[str] = None
    key_path: Optional[str] = None
    ca_path: Optional[str] = None
    mock: bool = True


class SwishClient:
    def __init__(self, cfg: SwishConfig):
        self.cfg = cfg

    def _requests_kwargs(self) -> Dict[str, Any]:
        if not self.cfg.cert_path or not self.cfg.key_path:
            raise RuntimeError("Swish mTLS requires cert and key paths")
        kwargs: Dict[str, Any] = {
            "timeout": 10,
            "cert": (self.cfg.cert_path, self.cfg.key_path),
            "verify": self.cfg.ca_path or True,
        }
        return kwargs

    def create_payment_request(
        self,
        amount_sek: int,
        message: str,
        callback_url_placeholder: Optional[str] = None,
        payer_alias: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create Swish payment request.

        In mock mode, returns synthetic identifiers used by local booking flows.
        In non-mock mode, performs a Commerce API call over mTLS.
        """
        instruction_uuid = str(uuid.uuid4())
        token = str(uuid.uuid4()).replace("-", "")
        request_id = instruction_uuid

        callback_url = callback_url_placeholder or self.cfg.callback_url
        swish_app_url = f"swish://paymentrequest?token={token}&callbackurl={callback_url}"

        if not self.cfg.mock:
            endpoint = f"{self.cfg.base_url.rstrip('/')}/api/v2/paymentrequests/{instruction_uuid}"
            payload = {
                "payeePaymentReference": request_id,
                "callbackUrl": callback_url,
                "payeeAlias": self.cfg.merchant_alias,
                "amount": f"{int(amount_sek)}",
                "currency": "SEK",
                "message": message[:50],
            }
            response = requests.put(
                endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
                **self._requests_kwargs(),
            )
            if int(response.status_code or 0) >= 300:
                raise RuntimeError(
                    f"Swish create payment failed: {response.status_code} {response.text[:200]}"
                )

        return {
            "instruction_uuid": instruction_uuid,
            "token": token,
            "request_id": request_id,
            "swish_app_url": swish_app_url,
        }

    def get_payment_request(self, request_id: str) -> Dict[str, Any]:
        if not self.cfg.mock:
            endpoint = f"{self.cfg.base_url.rstrip('/')}/api/v2/paymentrequests/{request_id}"
            response = requests.get(
                endpoint,
                headers={"Accept": "application/json"},
                **self._requests_kwargs(),
            )
            if int(response.status_code or 0) >= 300:
                raise RuntimeError(f"Swish get payment failed: {response.status_code} {response.text[:200]}")
            payload = response.json() if response.text else {}
            status = str(payload.get("status") or "PENDING").upper()
            return {"id": request_id, "status": status}
        return {"id": request_id, "status": "PENDING"}

    def get_qr_svg(self, token: str) -> str:
        return (
            "<svg xmlns='http://www.w3.org/2000/svg' width='200' height='200'>"
            f"<text x='10' y='100'>token:{token}</text></svg>"
        )
