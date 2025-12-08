from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List

@dataclass
class Holiday:
    name: str
    emoji: str
    start_month: int
    start_day: int
    end_month: int
    end_day: int

    def is_active(self, date: datetime) -> bool:
        # Simple case: start and end in same year
        if self.start_month < self.end_month or (self.start_month == self.end_month and self.start_day <= self.end_day):
             start_date = date.replace(month=self.start_month, day=self.start_day)
             end_date = date.replace(month=self.end_month, day=self.end_day)
             return start_date.date() <= date.date() <= end_date.date()
        else:
            # Cross-year case (e.g. Dec 25 to Jan 5)
            # If current month is start_month or later, treat as end of year
            if date.month >= self.start_month:
                start_date = date.replace(month=self.start_month, day=self.start_day)
                # end date is in next year, so we check if date >= start
                return date.date() >= start_date.date()
            # If current month is end_month or earlier, treat as start of year
            elif date.month <= self.end_month:
                end_date = date.replace(month=self.end_month, day=self.end_day)
                # start date was in prev year, so we check if date <= end
                return date.date() <= end_date.date()
            return False

HOLIDAYS: List[Holiday] = [
    Holiday("New Year", "🎄", 12, 20, 1, 15),
    Holiday("Valentine's Day", "💖", 2, 13, 2, 15),
    Holiday("Halloween", "🎃", 10, 25, 11, 1),
]

def get_holiday_emoji(date: datetime) -> Optional[str]:
    """
    Returns the emoji for the holiday active on the given date, if any.
    Returns None if no holiday is active.
    """
    for holiday in HOLIDAYS:
        if holiday.is_active(date):
            return holiday.emoji
    return None
