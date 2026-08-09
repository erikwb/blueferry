"""ANCS spec constants — wire format identifiers and enum values.

Ported from bmh129/ancs4linux/observer/ancs/constants.py
(GPL-2.0-compatible). Apple's ANCS spec revision 2014-10-20.
"""

USHORT_MAX = (2 ** 16) - 1
UINT_MAX = (2 ** 32) - 1

# ANCS bundle identifier used by Apple's Messages app. Only this source is
# retained, because its title/subtitle provide group metadata missing in MAP.
MESSAGES_APP_ID = "com.apple.MobileSMS"

# Service and characteristic UUIDs
ANCS_SERVICE_UUID = "7905f431-b5ce-4e99-a40f-4b1e122d00d0"
NOTIFICATION_SOURCE_CHAR = "9fbf120d-6301-42d9-8c58-25e699a21dbd"
CONTROL_POINT_CHAR       = "69d1d8f3-45e1-49a8-9821-9bbdfdaad9d9"
DATA_SOURCE_CHAR         = "22eac6e9-24d6-4bb5-be44-b36ace7c7bfb"
ANCS_CHAR_UUIDS = frozenset({
    NOTIFICATION_SOURCE_CHAR,
    CONTROL_POINT_CHAR,
    DATA_SOURCE_CHAR,
})


class CategoryID:
    Other = 0
    IncomingCall = 1
    MissedCall = 2
    Voicemail = 3
    Social = 4
    Schedule = 5
    Email = 6
    News = 7
    HealthAndFitness = 8
    BusinessAndFinance = 9
    Location = 10
    Entertainment = 11


CATEGORY_NAMES = {
    0: "Other",
    1: "IncomingCall",
    2: "MissedCall",
    3: "Voicemail",
    4: "Social",
    5: "Schedule",
    6: "Email",
    7: "News",
    8: "HealthAndFitness",
    9: "BusinessAndFinance",
    10: "Location",
    11: "Entertainment",
}


class EventID:
    NotificationAdded = 0
    NotificationModified = 1
    NotificationRemoved = 2


class EventFlag:
    Silent = 1 << 0
    Important = 1 << 1
    PreExisting = 1 << 2
    PositiveAction = 1 << 3
    NegativeAction = 1 << 4


class CommandID:
    GetNotificationAttributes = 0
    GetAppAttributes = 1


class NotificationAttributeID:
    AppIdentifier = 0
    Title = 1
    Subtitle = 2
    Message = 3


class AppAttributeID:
    DisplayName = 0
