from users import USERS


def display_name(user_id):
    return USERS[user_id]["name"].title()
