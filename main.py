import json

import pymongo
import pytz
import os

import uvicorn
from fastapi import FastAPI, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from typing import Optional, List, Union
from datetime import datetime, timedelta
from bson.objectid import ObjectId
from starlette.responses import JSONResponse, HTMLResponse
from deps import decode_token

from socket_manager import manager
from db import cards, stats, users, chat_rooms, create_chat
from utils import verify_password, get_hashed_password, create_access_token, get_messages_from_chat
from deps import get_current_user
from test_file import html
from socket_manager import ConnectionManager

app = FastAPI(docs_url="/")

timezone = pytz.timezone('Europe/Moscow')

SECRET_KEY = os.environ.get('mobile_secret_code')
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30


class Card(BaseModel):
    title: str
    date: int  # timestamp
    counter: int


class UpdateCard(BaseModel):
    title: Optional[str]
    counter: Optional[int]
    date: int  # timestamp


class ResponseCard(BaseModel):
    id: str
    title: str
    date: int  # timestamp
    counter: int
    user: str


class AuthUser(BaseModel):
    username: str = Field(max_length=20, min_length=6)
    password: str = Field(max_length=30, min_length=6)


@app.get('/api/get_cards', tags=['cards'], responses={
    200: {
        'description': 'Gives back all cards',
        'content': {
            'application/json': {
                'example': {'list of cards'}
            }
        }
    },
    404: {
        'description': 'user not found',
        'content': {
            'application/json': {
                'example': {'message': 'user not found'}
            }
        }
    }
})
async def get_cards(user: str = Depends(get_current_user)):
    if users.find_one({'username': user}) is None:
        return JSONResponse(status_code=404, content={'message': 'user not found'})
    cursor = cards.find({'is_deleted': False, 'user': user}).sort("date", pymongo.DESCENDING)

    result = []

    for card in cursor:
        card['id'] = str(card['_id'])  # converting id from ObjectId to string

        current_time = datetime.now(timezone).replace(tzinfo=None)

        difference = int((current_time - card['viewed']).total_seconds() // 3600)
        if difference > 24 or current_time.day != card[
            'viewed'].day:  # checking on different days and adding to statistics
            stats.update_one({'card': str(card['_id'])},
                             {'$set': {str(card['viewed'].strftime('%Y-%m-%d')): card['counter']}})
            delta = current_time - card['viewed']
            if delta.days > 1:
                data_update = dict()
                for i in range(1, delta.days):
                    day = datetime.strftime(card['viewed'] + timedelta(days=i), '%Y-%m-%d')
                    data_update.update({day: 0})
                stats.update_one({'card': str(card['_id'])}, {'$set': data_update})

            card['counter'] = 0
            cards.update_one({'_id': card['_id']}, {'$set': {'counter': 0}})

        cards.update_one({'_id': card['_id']}, {'$set': {'viewed': current_time}})

        del card['is_deleted'], card['_id'], card['viewed']
        result.append(card)

    return result


@app.post('/api/add_card', tags=['cards'], response_model=ResponseCard)
async def add_card(card: Card, user: str = Depends(get_current_user)):
    if users.find_one({'username': user}) is None:
        return JSONResponse(status_code=404, content={'message': 'user not found'})

    card = card.dict()
    card['is_deleted'] = False
    card['user'] = user
    card['viewed'] = datetime.now(timezone)
    cards.insert_one(card)

    card['id'] = str(card['_id'])  # converting id from ObjectId to string
    del card['is_deleted'], card['_id'], card['viewed']

    stat = {
        'card': card['id']
    }
    stats.insert_one(stat)

    return card


@app.delete(
    '/api/delete_card/{card_id}', tags=['cards'],
    responses={
        404: {
            "description": "The card was not found",
            "content": {
                "application/json": {
                    "example": {"message": "non-existent card"}
                }
            }
        },
        200: {
            "description": "Successful removal",
            "content": {
                "application/json": {
                    "example": {"message": "deleted"}
                }
            },
        },
    },
)
async def delete_card(card_id: str, user: str = Depends(get_current_user)):
    if users.find_one({'username': user}) is None:
        return JSONResponse(status_code=404, content={'message': 'user not found'})

    try:
        cards.update_one({'_id': ObjectId(card_id), 'user': user}, {'$set': {'is_deleted': True}})
    except:
        return JSONResponse(status_code=404, content={'message': 'non-existent card'})

    return {"message": "deleted"}


@app.put('/api/update_card/{card_id}', tags=['cards'], response_model=ResponseCard, responses={
    404: {
        "description": "The card was not found",
        "content": {
            "application/json": {
                "example": {'message': 'non-existent card'}
            }
        }
    }
})
async def update_card(card_id: str, card: UpdateCard, user: str = Depends(get_current_user)):
    if users.find_one({'username': user}) is None:
        return JSONResponse(status_code=404, content={'message': 'user not found'})

    card = card.dict(exclude_unset=True)  # getting a dictionary with only the entered fields

    try:
        cards.update_one({'_id': ObjectId(card_id), 'user': user}, {'$set': card})
    except:
        return JSONResponse(status_code=404, content={'message': 'non-existent card'})

    new_card = cards.find_one(ObjectId(card_id))

    new_card['id'] = str(new_card['_id'])
    del new_card['_id'], new_card['is_deleted'], new_card['viewed']

    return new_card


@app.get('/api/get_stat/{card_id}', tags=['cards'], responses={
    404: {
        "description": "The card was not found",
        "content": {
            "application/json": {
                "example": {'message': 'non-existent card'}
            }
        }
    },
    200: {
        'description': 'Gives back all cards',
        'content': {
            'application/json': {
                'example': {'list of stat'}
            }
        }
    }
})
async def get_stat(card_id: str, user: str = Depends(get_current_user)):
    stat = stats.find_one({'card': card_id})
    if stat is None:
        return JSONResponse(status_code=404, content={'message': 'non-existent card'})

    del stat['_id'], stat['card']

    result = []
    for key, value in stat.items():
        result.append({'date': key, 'counter': value})

    return result


@app.post('/api/registration', tags=['auth'], responses={
    409: {
        "description": "User already exists",
        "content": {
            "application/json": {
                "example": {'message': 'nickname is taken'}
            }
        }
    },
    200: {
        'description': 'Registration completed successfully',
        'content': {
            'application/json': {
                'example': {'message': 'registration completed successfully'}
            }
        }
    }
})
async def registration(user: AuthUser):
    if users.find_one({'username': user.username}) is not None:
        return JSONResponse(status_code=409, content={'message': 'nickname is taken'})

    user = user.dict()
    user['password'] = get_hashed_password(user['password'])
    users.insert_one(user)

    return JSONResponse(status_code=200, content={'message': 'registration completed successfully'})


@app.post('/api/login', tags=['auth'], responses={
    404: {
        "description": "User not found",
        "content": {
            "application/json": {
                "example": {'message': 'user not found'}
            }
        }
    },
    200: {
        'description': 'Login completed successfully',
        'content': {
            'application/json': {
                'example': {'access_token': 'string of token'}
            }
        }
    },
    401: {
        'description': 'Wrong password',
        'content': {
            'application/json': {
                'example': {'message': 'wrong password'}
            }
        }
    }
})
async def login(user: AuthUser):
    data_user = users.find_one({'username': user.username})
    if data_user is None:
        return JSONResponse(status_code=404, content={'message': 'user not found'})

    if not verify_password(user.password, data_user['password']):
        return JSONResponse(status_code=401, content={'message': 'wrong password'})

    return JSONResponse(status_code=200, content={
        "access_token": create_access_token(user.username)
    })


@app.get('/api/get_current_user', tags=['auth'], responses={
    200: {
        'description': 'Success login',
        'content': {
            'application/json': {
                'example': {'username': 'string'}
            }
        }
    },
    401: {
        'description': 'Not authenticated',
        'content': {
            'application/json': {
                'example': {"detail": "Not authenticated"}
            }
        }
    },
})
async def me(user: str = Depends(get_current_user)):
    return {'username': user}


@app.get('/api/ws_test')
async def ws_test():
    return HTMLResponse(html)


@app.websocket("/api/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            if 'token' in data.keys():
                user = await decode_token(data['token'])
                manager.authorized_connections.append({'username': user, 'websocket': websocket})
                await websocket.send_text("successful authorization")
            else:
                if not any(d['websocket'] == websocket for d in manager.authorized_connections):
                    await websocket.send_text('websocket not authorized')
                else:
                    receiver = data['receiver']
                    sender = data['sender']
                    message = data['message']
                    await manager.send_personal_message(receiver, sender, message)
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@app.get('/api/chat_users', tags=['chat'], responses={
    200: {
        'description': 'Gives back chats the user has participated in',
        'content': {
            'application/json': {
                'example': [
                    {
                        'username': 'bluefqcebaby',
                        'last_message': {
                            "from": "bluefqcebaby",
                            "to": "Nikita",
                            "message": "Andrey daunik",
                            "time": 1668698229591
                        }
                    },
                    {
                        'username': 'Viktor',
                        'last_message': {
                            "from": "Nikita",
                            "to": "Viktor",
                            "message": "Andrey idiot",
                            "time": 1668698229591
                        }
                    }
                ]
            }
        }
    },
})
async def chat_users(user: str = Depends(get_current_user)):
    chats = chat_rooms.find({'members': {'$all': [user]}}, {'_id': 0})

    data = list()
    for chat in chats:
        last_message = chat['messages'][-1]  # last chat message
        if last_message['from'] == user:
            last_message['is_myself'] = True
        else:
            last_message['is_myself'] = False
        del last_message['from'], last_message['to']
        data.append({
            'username': [el for el in chat['members'] if el != user][0],  # who is chatting with
            'last_message': last_message

        })

    return data


@app.get('/api/get_chat/{user2}', tags=['chat'], responses={
    200: {
        'description': 'Gives all messages of selected chat',
        'content': {
            'application/json': {
                'example': [{
                    "from": "killer",
                    "to": "Nikita",
                    "message": "+"
                }, {
                    "from": "killer",
                    "to": "Nikita",
                    "message": "+"
                }]
            }
        }
    },
})
async def get_chat(user2: str, user: str = Depends(get_current_user)):
    cursor = chat_rooms.find_one({'members': {'$all': [user, user2]}})
    if cursor is None:
        return JSONResponse(status_code=200, content=[])
    else:
        data = get_messages_from_chat(cursor)

    return JSONResponse(status_code=200, content=data)


@app.get('/api/chat_search/{search}', tags=['chat'], responses={
    200: {
        'description': 'Return a list of users',
        'content': {
            'application/json': {
                'example': [
                    {
                        "username": "killer"
                    },
                    {
                        "username": "e.v.kartashova"
                    }
                ]
            }
        }
    },
})
async def search_chat(search: str, user: str = Depends(get_current_user)):
    return list(users.find({'username': {'$regex': search, '$options': 'i', '$ne': user}}, {'_id': 0, 'password': 0}))
