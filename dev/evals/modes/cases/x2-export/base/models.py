from dataclasses import asdict, dataclass


@dataclass
class Customer:
    id: int
    name: str
    email: str
    ssn: str

    def to_public(self):
        data = asdict(self)
        data.pop("ssn")
        return data
