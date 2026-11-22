from pydantic import BaseModel, ConfigDict, Field, field_validator

from .contracts import FloorHourInput, validate_clock


class CorrectionInput(FloorHourInput):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    store_id: int = Field(gt=0)
    window_start: datetime
    new_count: int = Field(ge=0, le=300)
    reason: str = Field(min_length=5, max_length=120)

    @field_validator('window_start')
    @classmethod
    def window_boundary(cls, value):
        # 与观测窗口一致，按 15 分钟对齐且使用上海本地时间。
        return validate_clock(value, 15)
