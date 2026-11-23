from photo_organizer.auth.otp_sender import OtpSender


class FakeOtpSender(OtpSender):
    """Delivery fake retaining OTPs only in test-process memory."""

    def __init__(self) -> None:
        self._codes: dict[str, str] = {}

    async def send_code(self, phone_e164: str, code: str) -> None:
        self._codes[phone_e164] = code

    def last_code_for(self, phone_e164: str) -> str:
        return self._codes[phone_e164]
