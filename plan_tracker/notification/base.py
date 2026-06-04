"""Notification channel abstract base class."""

from abc import ABC, abstractmethod


class NotificationChannel(ABC):
    """Abstract interface for notification delivery."""

    @abstractmethod
    def send(
        self,
        message: str,
        plan_name: str,
        milestone_title: str,
        milestone_id: str,
    ) -> bool:
        """Send a notification. Returns True on success."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this channel is properly configured and ready."""
        ...
