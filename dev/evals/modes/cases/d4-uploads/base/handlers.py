from names import safe_name
from storage import save_upload


def handle_upload(request):
    return save_upload(safe_name(request["filename"]), request["body"])
