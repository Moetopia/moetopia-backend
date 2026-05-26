from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from tortoise.exceptions import DoesNotExist, IntegrityError


class MoetopiaException(Exception):
    """Moetopia 业务异常基类"""
    def __init__(self, message: str, code: int = 400):
        self.message = message
        self.code = code
        super().__init__(message)


class NotFoundError(MoetopiaException):
    def __init__(self, resource: str = "Resource"):
        super().__init__(f"{resource} not found", code=404)


class ForbiddenError(MoetopiaException):
    def __init__(self, message: str = "Operation not permitted"):
        super().__init__(message, code=403)


class AuthenticationError(MoetopiaException):
    def __init__(self, message: str = "Authentication required"):
        super().__init__(message, code=401)


class ConflictError(MoetopiaException):
    def __init__(self, message: str = "Resource already exists"):
        super().__init__(message, code=409)


# -------------------------------------------------------------------
# FastAPI 异常处理器注册函数
# -------------------------------------------------------------------

def register_exception_handlers(app):
    @app.exception_handler(MoetopiaException)
    async def moetopia_exception_handler(request: Request, exc: MoetopiaException):
        return JSONResponse(
            status_code=exc.code,
            content={"code": exc.code, "message": exc.message, "data": None},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        errors = [
            {"field": ".".join(str(l) for l in e["loc"]), "msg": e["msg"]}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"code": 422, "message": "Validation error", "data": errors},
        )

    @app.exception_handler(DoesNotExist)
    async def does_not_exist_handler(request: Request, exc: DoesNotExist):
        return JSONResponse(
            status_code=404,
            content={"code": 404, "message": str(exc), "data": None},
        )

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(request: Request, exc: IntegrityError):
        return JSONResponse(
            status_code=409,
            content={"code": 409, "message": "Database integrity error", "data": None},
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.status_code, "message": detail, "data": None},
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        import logging
        logging.getLogger(__name__).exception("Unhandled exception")
        return JSONResponse(
            status_code=500,
            content={"code": 500, "message": "Internal server error", "data": None},
        )
