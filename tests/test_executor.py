# ruff: noqa: E501 (lines copied verbatim from a real build log)
from bug_investigator.executor import parse_failures

# Excerpt of a real `docker build --progress=plain` of resourceBooking `bugset-1`.
RED_LOG = """\
#14 16.87 [INFO] Running kg.angryelizar.resourcebooking.service.BookingServiceTest
#14 17.12 2026-11-22T15:48:00.162Z ERROR 68 --- [main] k.a.r.service.impl.BookingServiceImpl    : Есть перекрытие по брони!
#14 17.19 [ERROR] Tests run: 6, Failures: 0, Errors: 1, Skipped: 0, Time elapsed: 0.362 s <<< FAILURE! -- in kg.angryelizar.resourcebooking.service.BookingServiceTest
#14 17.19 [ERROR] kg.angryelizar.resourcebooking.service.BookingServiceTest.bookingRightAfterConfirmedOneIsAllowed -- Time elapsed: 0.014 s <<< ERROR!
#14 17.19 kg.angryelizar.resourcebooking.exceptions.ResourceException: Бронирование недоступно
#14 17.19 	at kg.angryelizar.resourcebooking.service.impl.BookingServiceImpl.create(BookingServiceImpl.java:76)
#14 17.19
#14 17.20 [ERROR] kg.angryelizar.resourcebooking.service.PaymentServiceTest.amountIsPerHour -- Time elapsed: 0.02 s <<< FAILURE!
#14 17.20 org.opentest4j.AssertionFailedError: expected: <200.00> but was: <20.00>
#14 17.20 	at org.junit.jupiter.api.AssertionFailureBuilder.build(AssertionFailureBuilder.java:151)
#14 17.21 [ERROR] kg.angryelizar.resourcebooking.service.ResourceServiceTest.rateIsUpdated(int)[2] -- Time elapsed: 0.01 s <<< FAILURE!
#14 17.21 org.opentest4j.AssertionFailedError: expected: <300> but was: <100>
#14 17.43 [ERROR] Errors:
#14 17.43 [ERROR]   BookingServiceTest.bookingRightAfterConfirmedOneIsAllowed:77 » Resource Бронирование недоступно
#14 17.43 [INFO] BUILD FAILURE
17.44 [ERROR] kg.angryelizar.resourcebooking.service.BookingServiceTest.bookingRightAfterConfirmedOneIsAllowed -- Time elapsed: 0.014 s <<< ERROR!
17.44 kg.angryelizar.resourcebooking.exceptions.ResourceException: Бронирование недоступно
"""


def test_failed_tests_and_their_exceptions_are_read_from_surefire_output():
    failures = parse_failures(RED_LOG)

    assert failures.tests == [
        "BookingServiceTest.bookingRightAfterConfirmedOneIsAllowed",
        "PaymentServiceTest.amountIsPerHour",
        "ResourceServiceTest.rateIsUpdated",
    ]
    # Assertion errors say nothing about the cause, so they are not search terms.
    assert failures.exceptions == ["ResourceException"]


def test_green_log_has_no_failures():
    failures = parse_failures("#14 17.43 [INFO] Tests run: 12, Failures: 0, Errors: 0\n")

    assert failures.tests == []
    assert failures.exceptions == []
