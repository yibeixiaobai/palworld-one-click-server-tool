from palworld_console.player_center import PlayerCenterController


def test_player_center_requires_sync_before_selection_and_preserves_draft_on_failure():
    controller = PlayerCenterController()
    try:
        controller.select("uid-1")
    except RuntimeError:
        pass
    else:
        raise AssertionError("未同步时不应允许选择角色")

    controller.begin_sync("instance-1")
    controller.complete_sync("instance-1", [{"player_uid": "uid-1"}], [], "Level.sav", True)
    controller.select("uid-1")
    session = controller.session("instance-1", "uid-1")
    session.stage("players[0].level", 10, 20, "玩家等级", "player", "uid-1")
    controller.mark_save_failure("健康检查失败")
    assert controller.retry_available is True
    assert controller.pending_count("instance-1", "uid-1") == 1


def test_player_center_mark_saved_clears_only_selected_session():
    controller = PlayerCenterController()
    controller.complete_sync("instance-1", [], [], "Level.sav", True)
    first = controller.session("instance-1", "uid-1"); second = controller.session("instance-1", "uid-2")
    first.stage("players[0].level", 1, 2, "玩家等级", "player", "uid-1")
    second.stage("players[0].level", 1, 3, "玩家等级", "player", "uid-2")
    controller.mark_saved("instance-1", "uid-1")
    assert controller.pending_count("instance-1", "uid-1") == 0
    assert controller.pending_count("instance-1", "uid-2") == 1
