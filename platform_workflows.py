"""One capability registry for the service, adapter and platform cards.

Fixture-only implementations are never included in PREPARE_PLATFORMS. Account
login, installed dependencies and historical automatic posts do not enable them.
"""

PREPARE_PLATFORMS = ("douyin", "xiaohongshu", "youtube", "bilibili")
BILIBILI_CATEGORIES = ('影视','娱乐','音乐','舞蹈','动画','绘画','鬼畜','游戏','资讯','知识','人工智能','科技数码','汽车','时尚美妆','家装房产','户外潮流','健身','体育运动','手工','美食','小剧场','旅游出行','三农','动物','亲子','健康','情感','vlog','生活兴趣','生活经验')
BILIBILI_DECLARATIONS = {"none":"内容无需标注","ai":"含AI生成内容","fiction":"含虚构演绎内容","marketing":"内容含营销信息","opinion":"个人观点，仅供参考","repost":"内容为转载"}
FIXTURE_PLATFORMS = ("tencent",)

LIMITATIONS = {
    "douyin": ["人工确认流程已接入；真实素材和封面流程仍需在当前账号验证。", "同一平台换号后复用相同视频暂未支持。"],
    "xiaohongshu": ["人工确认流程已接入；视频上传完成后才设置封面。AI内容声明尚未适配。", "同一平台换号后复用相同视频暂未支持。"],
    "youtube": ["人工确认流程已接入；必须核对目标频道。Chrome会话迁移仍需在线验证，尚未完成当前账号实测。"],
    "bilibili": ["2026-10-12 已在当前 Mac 和账号用8秒测试视频完整验收到投稿前。", "使用已扫码账号打开专用网页，填写后保留窗口，由本人点击立即投稿。", "指定封面同步用于4:3和16:9比例，请在发布前检查裁切。"],
    "tencent": ["新增视频号上传、填写、封面与原页接续的合成测试适配，生产入口仍关闭。",
                "此前站点访问限制仍有效；本轮未访问真实视频号，也不通过替代工具绕过限制。"],
}


def workflow_info(platform):
    supported = platform in PREPARE_PLATFORMS
    fixture = platform in FIXTURE_PLATFORMS
    return {
        "workflow_status": "implemented" if platform == "bilibili" else "unverified" if supported else "fixture_only" if fixture else "manual_only",
        "prepare_supported": supported,
        "implementation_scope": "production_local_verified" if platform == "bilibili" else "production_unverified" if supported else "fixture_only" if fixture else "not_implemented",
        "acceptance_status": "local_account_verified_2026_10_12" if platform == "bilibili" else "real_account_pending" if supported else "fixture_only" if fixture else "not_started",
        "live_access_restricted": platform == "tencent",
        "known_limitations": list(LIMITATIONS.get(platform, ["人工确认流程尚未适配，当前不会执行自动提交。"])),
        **({"categories": list(BILIBILI_CATEGORIES), "declarations": BILIBILI_DECLARATIONS} if platform == "bilibili" else {}),
    }
