class DomainError(Exception):
    """只携带安全错误码的领域异常基类。"""


class InvalidSignature(DomainError):
    pass


class EventConflict(DomainError):
    pass


class MaterialUsageReferenceNotFound(DomainError):
    pass


class ObjectValidationFailed(DomainError):
    pass


class TemplateVersionConflict(DomainError):
    pass
