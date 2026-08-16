from unittest.mock import patch

from app import telegram_access as ta


def test_grant_premium_access_no_config():
    result = ta.grant_premium_access("", "", name_label="x")
    assert result.ok is False
    assert "not configured" in result.error


def test_grant_premium_access_success():
    with patch.object(ta, "_call") as mock_call:
        mock_call.return_value = (True, {"ok": True, "result": {"invite_link": "https://t.me/+abc123"}})
        result = ta.grant_premium_access("tok", "-100999", name_label="sub-1")
    assert result.ok is True
    assert result.invite_link == "https://t.me/+abc123"
    mock_call.assert_called_once()
    method_arg = mock_call.call_args[0][1]
    assert method_arg == "createChatInviteLink"


def test_grant_premium_access_failure_surfaces_description():
    with patch.object(ta, "_call") as mock_call:
        mock_call.return_value = (False, {"ok": False, "description": "Not enough rights"})
        result = ta.grant_premium_access("tok", "-100999")
    assert result.ok is False
    assert result.error == "Not enough rights"


def test_revoke_premium_access_success():
    with patch.object(ta, "_call") as mock_call:
        mock_call.side_effect = [
            (True, {"ok": True, "result": True}),   # banChatMember
            (True, {"ok": True, "result": True}),   # unbanChatMember
        ]
        result = ta.revoke_premium_access("tok", "-100999", "12345")
    assert result.ok is True
    assert mock_call.call_count == 2
    assert mock_call.call_args_list[0][0][1] == "banChatMember"
    assert mock_call.call_args_list[1][0][1] == "unbanChatMember"


def test_revoke_premium_access_ban_fails():
    with patch.object(ta, "_call") as mock_call:
        mock_call.return_value = (False, {"ok": False, "description": "user not found"})
        result = ta.revoke_premium_access("tok", "-100999", "12345")
    assert result.ok is False
    assert result.error == "user not found"


def test_revoke_premium_access_unban_fails_still_reports_success():
    with patch.object(ta, "_call") as mock_call:
        mock_call.side_effect = [
            (True, {"ok": True, "result": True}),
            (False, {"ok": False, "description": "some transient error"}),
        ]
        result = ta.revoke_premium_access("tok", "-100999", "12345")
    assert result.ok is True  # member was removed; that was the actual goal
