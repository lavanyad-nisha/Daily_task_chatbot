"""Jibble-to-employee matching rules. Pure."""

from daily_task_bot.domain.jibble_matching import match_by_email


def test_exact_email_match_is_case_and_space_insensitive():
    p = match_by_email([(1, " Asha@Example.com ")], [("J1", "asha@example.com")])
    assert p.matches == [(1, "J1")]
    assert p.counts()["matched"] == 1


def test_employee_without_jibble_person_is_unmatched():
    p = match_by_email([(1, "a@example.com")], [("J1", "b@example.com")])
    assert p.matches == [] and p.employees_unmatched == [1]


def test_duplicate_jibble_email_is_ambiguous_not_guessed():
    p = match_by_email([(1, "a@example.com")], [("J1", "a@example.com"), ("J2", "A@example.com")])
    assert p.matches == []
    assert p.employees_ambiguous == [1]
    assert set(p.jibble_ambiguous) == {"J1", "J2"}


def test_duplicate_employee_email_is_ambiguous():
    p = match_by_email([(1, "a@example.com"), (2, "a@example.com")], [("J1", "a@example.com")])
    assert p.matches == []
    assert set(p.employees_ambiguous) == {1, 2}


def test_jibble_people_without_email_are_counted_not_matched():
    p = match_by_email([(1, "a@example.com")], [("J1", None), ("J2", ""), ("J3", "a@example.com")])
    assert p.jibble_without_email == 2
    assert p.matches == [(1, "J3")]


def test_employee_without_email_is_unmatched():
    p = match_by_email([(1, None)], [("J1", "a@example.com")])
    assert p.employees_unmatched == [1]
