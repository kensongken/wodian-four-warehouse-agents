"""OTP delivery adapters.  Neither adapter logs OTP values."""

import asyncio
import logging
from typing import Protocol

from tencentcloud.common.credential import Credential  # type: ignore[import-untyped]
from tencentcloud.common.profile.client_profile import (  # type: ignore[import-untyped]
    ClientProfile,
)
from tencentcloud.common.profile.http_profile import (  # type: ignore[import-untyped]
    HttpProfile,
)
from tencentcloud.sms.v20210111 import (  # type: ignore[import-untyped]
    models,
    sms_client,
)

from photo_organizer.config import Settings

logger = logging.getLogger(__name__)


class OtpSender(Protocol):
    async def send_code(self, phone_e164: str, code: str) -> None: ...


def _masked_phone(phone_e164: str) -> str:
    """Keep enough of a phone number for development delivery diagnostics."""
    if len(phone_e164) <= 4:
        return "*" * len(phone_e164)
    return f"{phone_e164[:3]}{'*' * (len(phone_e164) - 5)}{phone_e164[-2:]}"


class DevelopmentConsoleOtpSender:
    """Development-only sender which never exposes the OTP itself."""

    def __init__(self, settings: Settings) -> None:
        if settings.app_env != "development":
            msg = "the console OTP sender is permitted only in development"
            raise RuntimeError(msg)

    async def send_code(self, phone_e164: str, code: str) -> None:
        del code
        logger.info("OTP requested for destination %s", _masked_phone(phone_e164))


class TencentCloudOtpSender:
    """Tencent Cloud SMS sender configured exclusively from environment settings."""

    def __init__(self, settings: Settings) -> None:
        required = {
            "TENCENT_SECRET_ID": settings.tencent_secret_id,
            "TENCENT_SECRET_KEY": settings.tencent_secret_key,
            "TENCENT_SMS_SDK_APP_ID": settings.tencent_sms_sdk_app_id,
            "TENCENT_SMS_SIGN_NAME": settings.tencent_sms_sign_name,
            "TENCENT_SMS_TEMPLATE_ID": settings.tencent_sms_template_id,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            msg = "missing Tencent Cloud SMS environment configuration"
            raise ValueError(msg)
        self._secret_id = settings.tencent_secret_id
        self._secret_key = settings.tencent_secret_key
        self._sdk_app_id = settings.tencent_sms_sdk_app_id
        self._sign_name = settings.tencent_sms_sign_name
        self._template_id = settings.tencent_sms_template_id

    async def send_code(self, phone_e164: str, code: str) -> None:
        await asyncio.to_thread(self._send, phone_e164, code)

    def _send(self, phone_e164: str, code: str) -> None:
        credential = Credential(self._secret_id, self._secret_key)
        http_profile = HttpProfile()
        http_profile.endpoint = "sms.tencentcloudapi.com"
        profile = ClientProfile()
        profile.httpProfile = http_profile
        client = sms_client.SmsClient(credential, "ap-guangzhou", profile)
        request = models.SendSmsRequest()
        request.SmsSdkAppId = self._sdk_app_id
        request.SignName = self._sign_name
        request.TemplateId = self._template_id
        request.TemplateParamSet = [code]
        request.PhoneNumberSet = [phone_e164]
        client.SendSms(request)
