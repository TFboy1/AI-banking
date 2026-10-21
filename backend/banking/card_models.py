from pydantic import BaseModel, ConfigDict, Field


class AddCard(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    bank_name: str = Field(min_length=1, max_length=24)
    card_number: str = Field(pattern=r'^[0-9]{16,19}$')
