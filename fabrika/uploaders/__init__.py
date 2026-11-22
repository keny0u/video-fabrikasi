from .. import config


class DryRunUploader:
    def connected(self):
        return True

    def publish(self, post):
        print(f"[deneme] yüklendi sayıldı: {post.title}")
        return ""


def get_uploader(target):
    if config.DRY_RUN:
        return DryRunUploader()
    if target == "youtube":
        from .youtube import YouTubeUploader
        return YouTubeUploader(shorts=False)
    if target == "youtube_shorts":
        from .youtube import YouTubeUploader
        return YouTubeUploader(shorts=True)
    if target == "tiktok":
        from .tiktok import TikTokUploader
        return TikTokUploader()
    if target == "instagram_reels":
        from .instagram import InstagramUploader
        return InstagramUploader()
    raise KeyError(target)
