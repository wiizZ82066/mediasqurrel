"""Local-only mail settings. SMTP credentials are write-only and never go into the media DB."""
import asyncio
import re
import sqlite3

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from . import mail_delivery, mail_media, mail_store

router = APIRouter(prefix='/api/settings/mail')


def private(data, status=200):
    return JSONResponse(data, status_code=status, headers={'Cache-Control': 'no-store'})


@router.get('')
def settings():
    try:
        return private(mail_store.public())
    except (OSError, sqlite3.Error):
        raise HTTPException(503, '无法读取本机的邮件设置，请检查数据目录是否可写，然后重启程序。') from None


@router.post('')
async def save(body: dict):
    if not all(isinstance(body.get(k), str) for k in ('sender', 'recipient', 'code')) or type(body.get('enabled')) is not bool:
        raise HTTPException(400, '请填写邮箱地址、授权码及自动发送选项。')
    try:
        return private(await mail_delivery.save(body['sender'], body['recipient'], body['code'], body['enabled']))
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@router.get('/sample')
def sample():
    try:
        return private(mail_media.sample())
    except ValueError as error:
        raise HTTPException(404, str(error)) from None


@router.post('/sample')
async def send_sample(body: dict):
    identity = body.get('task_id')
    if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,64}', identity):
        raise HTTPException(400, '请选择一条已下载的作品。')
    try:
        task = await asyncio.to_thread(mail_media.task_by_id, identity)
        await asyncio.to_thread(mail_media.resolve, task)
        key = await asyncio.to_thread(mail_store.queue_test, identity)
        mail_delivery.notify()
        return private({'id': key}, 202)
    except (OSError, HTTPException):
        raise HTTPException(404, '样本文件已不可用，请重新选择。') from None
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@router.post('/deliveries/{key}/retry')
async def retry(key: str, body: dict):
    try:
        await asyncio.to_thread(mail_store.retry, key, body.get('confirmed'))
        mail_delivery.notify()
        return private({'id': key}, 202)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
