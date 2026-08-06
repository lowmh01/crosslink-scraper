import {createClient} from '@supabase/supabase-js'

const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN
const API = `https: // api.telegram.org/bot${BOT_TOKEN}`
const sb = createClient(process.env.NEXT_PUBLIC_SUPABASE_URL, process.env.SUPABASE_SERVICE_ROLE_KEY)

const CHANNEL = '@jbsglink' // 改成你的 group username
const BOT_USERNAME = 'jbsglink_bot' // 改成你实际的 bot username

async function send(chatId, text) {
    await fetch(`${API}/sendMessage`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({chat_id: chatId, text, parse_mode: 'HTML', disable_web_page_preview: true}),
    })
}

async function isMember(userId) {
    const res = await fetch(`${API}/getChatMember`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({chat_id: CHANNEL, user_id: userId}),
    })
    const data = await res.json()
    const status = data?.result?.status
    return ['member', 'administrator', 'creator'].includes(status)
}

async function setAlert(chatId, rate) {
    await sb.from ('telegram_alerts')
        .update({is_active: false})
        .eq('chat_id', chatId)

    await sb.from ('telegram_alerts').insert({
        chat_id: chatId,
        target_rate: rate.toFixed(4),
        direction: 'above',
        is_active: true,
    })

    await send(chatId,
        `Alert set · 提醒已设定\n\n` +
        `Notify when CIMB rate ≥ ${rate.toFixed(4)}\n` +
        `当 CIMB 汇率 ≥ ${rate.toFixed(4)} 时通知你\n\n` +
        `Send /stop to stop · 发送 /stop 停止通知\n\n` +
        `———\n` +
        `分享给需要的朋友和家人 · Share with friends & family\n` +
        `@${BOT_USERNAME}`
    )
}

export async function POST(req) {
    const body = await req.json()
    const msg = body.message
    if (!msg?.chat?.id) return Response.json({ ok: true })

    const chatId = msg.chat.id
    const text = (msg.text || '').trim()

    if (msg.chat.type !== 'private') return Response.json({ ok: true })

    if (text === '/start') {
        await send(chatId,
            '<b>JB-SG Link · CIMB Rate Alert</b>\n' +
            'CIMB 汇率提醒\n\n' +
            'Get notified when CIMB SGD → MYR hits your target.\n' +
            '当 CIMB 汇率达到你的目标时通知你。\n\n' +
            '<b>How to use 使用方法：</b>\n\n' +
            '/alert 3.18\n' +
            'Set your target rate · 设定目标汇率\n' +
            'Or just type the rate · 或直接输入汇率，如 3.18\n\n' +
            '/status\n' +
            'Check your alerts · 查看提醒状态\n\n' +
            '/stop\n' +
            'Stop all notifications · 停止所有通知\n\n' +
            'You will be notified every 15 min while the rate stays above your target.\n' +
            '汇率高于目标期间，每 15 分钟通知一次。\n\n' +
            'CIMB updates rates on weekdays, 9 AM – 7 PM SGT.\n' +
            'CIMB 工作日 9AM–7PM（新加坡时间）更新汇率。\n\n' +
            'Join our group to use this bot · 加入群组即可使用\n' +
            'https://t.me/' + CHANNEL.replace('@', '') + '\n\n' +
            'jbsglink.com/exchange-rate'
        )
        return Response.json({ ok: true })
    }

    const member = await isMember(chatId)
    if (!member) {
        await send(chatId,
            'Please join our group first · 请先加入我们的群组\n\n' +
            'https://t.me/' + CHANNEL.replace('@', '') + '\n\n' +
            'Then send /start to get started\n' +
            '加入后发送 /start 开始使用'
        )
        return Response.json({ ok: true })
    }

    if (text.startsWith('/alert')) {
        const parts = text.split(/\s+/)
        if (parts.length !== 2) {
            await send(chatId,
                'Enter a target rate after /alert\n' +
                '请在 /alert 后输入目标汇率\n\n' +
                'Example 例子：/alert 3.18'
            )
            return Response.json({ ok: true })
        }
        const target = parseFloat(parts[1])
        if (isNaN(target) || target < 3.0 || target > 4.0) {
            await send(chatId,
                'Rate must be between 3.0 and 4.0\n' +
                '汇率需在 3.0 到 4.0 之间\n\n' +
                'Example 例子：/alert 3.18'
            )
            return Response.json({ ok: true })
        }

        await setAlert(chatId, target)

    } else if (text === '/status') {
        const { data } = await sb.from('telegram_alerts')
            .select('target_rate')
            .eq('chat_id', chatId)
            .eq('is_active', true)

        if (!data?.length) {
            await send(chatId,
                'No active alerts · 没有活跃的提醒\n\n' +
                'Send /alert 3.18 to set one\n' +
                '发送 /alert 3.18 设定一个'
            )
        } else {
            const rate = parseFloat(data[0].target_rate).toFixed(4)
            await send(chatId,
                `<b>Active alert 活跃提醒：</b>\n` +
                `  ≥ ${rate}\n\n` +
                'Send /stop to cancel · 发送 /stop 取消\n' +
                'Send /alert 3.20 to change · 发送 /alert 3.20 更改目标'
            )
        }

    } else if (text === '/stop') {
        await sb.from('telegram_alerts')
            .update({ is_active: false })
            .eq('chat_id', chatId)
        await send(chatId,
            'All notifications stopped · 所有通知已停止\n\n' +
            'Want to set a new alert? · 要重新设定提醒？\n' +
            'Send /start to see instructions\n' +
            '发送 /start 查看使用说明'
        )
    } else {
        const num = parseFloat(text)
        if (!isNaN(num) && num >= 3.0 && num <= 4.0) {
            await setAlert(chatId, num)
        } else {
            await send(chatId,
                'Send /start for instructions\n' +
                '发送 /start 查看使用说明'
            )
        }
    }

    return Response.json({ ok: true })
}