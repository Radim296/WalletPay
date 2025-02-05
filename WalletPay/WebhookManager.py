from wsgiref.util import request_uri
from fastapi import FastAPI, Request, HTTPException
from .types import Event
from typing import Any, Callable, Dict, List, Optional, Union
from . import WalletPayAPI, AsyncWalletPayAPI
import logging
import hmac
import base64


logging.basicConfig(level=logging.INFO)


class WebhookManager:
    """
    A manager for handling webhooks from WalletPay.

    Attributes:
        successful_callbacks (list): A list of callback functions to handle successful events.
        failed_callbacks (list): A list of callback functions to handle failed events.
        host (str): The host to run the FastAPI server on.
        port (int): The port to run the FastAPI server on.
        webhook_endpoint (str): The endpoint to listen for incoming webhooks.
        app (FastAPI): The FastAPI application instance.
        ALLOWED_IPS (set): A set of IP addresses allowed to send webhooks.
    """

    ALLOWED_IPS = {"172.255.248.29", "172.255.248.12", "127.0.0.1"}

    def __init__(
        self,
        client: Union[WalletPayAPI, AsyncWalletPayAPI],
        host: str = "0.0.0.0",
        port: int = 9123,
        webhook_endpoint: str = "/wp_webhook",
    ):
        """
        Initialize the WebhookManager.

        :param client:
        :param host: The host to run the FastAPI server on. Default is "0.0.0.0".
        :param port: The port to run the FastAPI server on. Default is 9123.
        :param webhook_endpoint: The endpoint to listen for incoming webhooks. Default is "/wp_webhook".
        """
        self.successful_callbacks: List[Callable] = []
        self.failed_callbacks: List[Callable] = []
        self.host: str = host
        self.port: int = port
        self.api_key: str = client.api_key
        if webhook_endpoint[0] != "/":
            self.webhook_endpoint: str = f"/{webhook_endpoint}"
        else:
            self.webhook_endpoint: str = webhook_endpoint

        self.app: FastAPI = FastAPI()
        self.client: Union[AsyncWalletPayAPI, WalletPayAPI] = client

    async def start(self):
        """
        Start the FastAPI server to listen for incoming webhooks.
        """
        if self.app:
            import uvicorn

            self.register_webhook_endpoint()
            logging.info(
                f"Webhook is listening at https://{self.host}:{self.port}{self.webhook_endpoint}"
            )
            runner = uvicorn.Server(
                config=uvicorn.Config(
                    self.app,
                    host=self.host,
                    port=self.port,
                    access_log=False,
                    log_level="error",
                )
            )
            await runner.serve()

    def successful_handler(self):
        """
        Decorator to register a callback function for handling successful events.

        :return: Decorator function.
        """

        def decorator(func):
            self.successful_callbacks.append(func)
            return func

        return decorator

    def failed_handler(self):
        """
        Decorator to register a callback function for handling failed events.

        :return: Decorator function.
        """

        def decorator(func):
            self.failed_callbacks.append(func)
            return func

        return decorator

    def __get_x_forwarded_for(self, headers: Dict[str, Any]) -> Optional[str]:
        """
        The function checks if any of the IP addresses in the "X-Forwarded-For" header are in the list of
        allowed IPs.

        :param headers: A dictionary containing the headers of an HTTP request
        :type headers: Dict[str, Any]
        :return: a boolean value. It returns True if any of the IP addresses in the "X-Forwarded-For" header
        are found in the ALLOWED_IPS list. Otherwise, it returns False.
        """
        if x_forwarded_for := headers.get("X-Forwarded-For"):
            forwarded_for_ips: List[str] = x_forwarded_for.split(", ")

            for ip in forwarded_for_ips:
                if ip in self.ALLOWED_IPS:
                    return ip

    def __get_client_ip(self, request: Request) -> str:
        """
        The function `__get_client_ip` retrieves the client's IP address from the request, checks if it is
        allowed, and returns it.

        :param request: The `request` parameter is of type `Request`, which is likely an object representing
        an HTTP request. It contains information about the request, such as headers, client host, etc
        :type request: Request
        :return: the client IP address as a string.
        """
        x_forwarded_for: Optional[str] = self.__get_x_forwarded_for(headers=request.headers)
        client_ip: str = request.client.host

        if x_forwarded_for:
            client_ip = x_forwarded_for
        elif client_ip not in self.ALLOWED_IPS:
            logging.info(f"IP {client_ip} not allowed")
            raise HTTPException(status_code=403, detail="IP not allowed")

        return client_ip

    async def _handle_webhook(self, request: Request):
        """
        Internal method to handle incoming webhooks.

        1. Verifies the IP address of the incoming request.
        2. Verifies the signature of the incoming request.
        3. Processes the webhook data.

        :param request: The incoming request object.
        :return: A dictionary with a message indicating the result of the webhook processing.
        """
        client_ip: str = self.__get_client_ip(request=request)

        logging.info(f"Incoming webhook from {client_ip}")

        data = await request.json()
        raw_body = await request.body()
        headers = request.headers

        signature = headers.get("Walletpay-Signature")
        timestamp = headers.get("WalletPay-Timestamp")
        method = request.method
        path = request.headers.get("X-Original-URI") or request.url.path
        message = f"{method}.{path}.{timestamp}.{base64.b64encode(raw_body).decode()}"

        expected_signature = hmac.new(
            bytes(self.api_key, "utf-8"),
            msg=bytes(message, "utf-8"),
            digestmod=hmac._hashlib.sha256,
        ).digest()

        expected_signature_b64 = base64.b64encode(expected_signature).decode()
        if not hmac.compare_digest(expected_signature_b64, signature):
            logging.info(
                f"Invalid signature. Expected: {expected_signature_b64} Get from header: {signature}"
            )
            raise HTTPException(status_code=400, detail="Invalid signature")

        event = Event(data[0])
        if event.type == "ORDER_PAID":
            for callback in self.successful_callbacks:
                await callback(event=event, client=self.client)
            return {"message": "Successful event processed!"}
        elif event.type == "ORDER_FAILED":
            for callback in self.failed_callbacks:
                await callback(event=event, client=self.client)
            return {"message": "Failed event processed!"}
        else:
            return {"message": "Webhook received with unknown status!"}

    def register_webhook_endpoint(self):
        """
        Register the webhook endpoint in the FastAPI application.
        """
        self.app.post(self.webhook_endpoint)(self._handle_webhook)
