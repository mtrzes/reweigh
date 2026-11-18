from users import USERS

_cache = {}


def display_name(user_id):
    if user_id not in _cache:
        _cache[user_id] = USERS[user_id]["name"].title()
    return _cache[user_id]
