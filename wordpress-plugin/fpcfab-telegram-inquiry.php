<?php
/**
 * Plugin Name: FPCFAB Telegram Inquiry
 * Description: Sends successful Contact Form 7 inquiries to Telegram and DingTalk without transferring customer attachments.
 * Version: 1.1.0
 * Author: FPCFAB
 */

if (!defined('ABSPATH')) {
    exit;
}

const FPCFAB_TG_TOKEN_OPTION = 'fpcfab_tg_bot_token';
const FPCFAB_TG_CHAT_OPTION = 'fpcfab_tg_chat_id';
const FPCFAB_TG_ENABLED_OPTION = 'fpcfab_tg_enabled';
const FPCFAB_DING_WEBHOOK_OPTION = 'fpcfab_ding_webhook';
const FPCFAB_DING_SECRET_OPTION = 'fpcfab_ding_secret';

register_activation_hook(__FILE__, 'fpcfab_tg_activate');

function fpcfab_tg_activate() {
    if (get_option(FPCFAB_TG_CHAT_OPTION, '') === '') {
        add_option(FPCFAB_TG_CHAT_OPTION, '', '', false);
    }
    if (get_option(FPCFAB_TG_ENABLED_OPTION, null) === null) {
        add_option(FPCFAB_TG_ENABLED_OPTION, '1', '', false);
    }
}

add_action('admin_menu', 'fpcfab_tg_admin_menu');

function fpcfab_tg_admin_menu() {
    add_submenu_page(
        'wpcf7',
        'Telegram / 钉钉询盘通知',
        'Telegram / 钉钉询盘通知',
        'manage_options',
        'fpcfab-telegram-inquiry',
        'fpcfab_tg_settings_page'
    );
}

function fpcfab_tg_settings_page() {
    if (!current_user_can('manage_options')) {
        return;
    }
    $configured = get_option(FPCFAB_TG_TOKEN_OPTION, '') !== '';
    $ding_configured = get_option(FPCFAB_DING_WEBHOOK_OPTION, '') !== '' && get_option(FPCFAB_DING_SECRET_OPTION, '') !== '';
    $chat_id = get_option(FPCFAB_TG_CHAT_OPTION, '');
    $enabled = get_option(FPCFAB_TG_ENABLED_OPTION, '1') === '1';
    $notice = isset($_GET['fpcfab_tg_notice']) ? sanitize_key(wp_unslash($_GET['fpcfab_tg_notice'])) : '';
    ?>
    <div class="wrap">
        <h1>Telegram / 钉钉询盘通知</h1>
        <p>Contact Form 7 成功发送邮件后，将客户询盘摘要同时推送到 Telegram 和钉钉。客户附件只显示文件名，不上传附件内容。</p>
        <?php if ($notice === 'saved') : ?>
            <div class="notice notice-success is-dismissible"><p>设置已保存。</p></div>
        <?php elseif ($notice === 'test_ok') : ?>
            <div class="notice notice-success is-dismissible"><p>测试消息发送成功。</p></div>
        <?php elseif ($notice === 'test_failed') : ?>
            <div class="notice notice-error is-dismissible"><p>测试消息发送失败，请检查 Token、Chat ID 或服务器网络。</p></div>
        <?php endif; ?>

        <form method="post" action="<?php echo esc_url(admin_url('admin-post.php')); ?>">
            <input type="hidden" name="action" value="fpcfab_tg_save">
            <?php wp_nonce_field('fpcfab_tg_save'); ?>
            <table class="form-table" role="presentation">
                <tr>
                    <th scope="row"><label for="fpcfab_tg_token">机器人 Token</label></th>
                    <td>
                        <input id="fpcfab_tg_token" name="bot_token" type="password" class="regular-text" autocomplete="off"
                               placeholder="<?php echo esc_attr($configured ? '已经配置；留空表示不修改' : '粘贴 BotFather 给你的 Token'); ?>">
                        <p class="description">Token 不会显示在页面上。</p>
                    </td>
                </tr>
                <tr>
                    <th scope="row"><label for="fpcfab_tg_chat">Chat ID</label></th>
                    <td><input id="fpcfab_tg_chat" name="chat_id" type="text" class="regular-text" value="<?php echo esc_attr($chat_id); ?>"></td>
                </tr>
                <tr>
                    <th scope="row"><label for="fpcfab_ding_webhook">钉钉 Webhook</label></th>
                    <td>
                        <input id="fpcfab_ding_webhook" name="ding_webhook" type="password" class="regular-text" autocomplete="off"
                               placeholder="<?php echo esc_attr($ding_configured ? '已经配置；留空表示不修改' : '粘贴完整 Webhook 地址'); ?>">
                        <p class="description">Webhook 不会显示在页面上。</p>
                    </td>
                </tr>
                <tr>
                    <th scope="row"><label for="fpcfab_ding_secret">钉钉加签密钥</label></th>
                    <td>
                        <input id="fpcfab_ding_secret" name="ding_secret" type="password" class="regular-text" autocomplete="off"
                               placeholder="<?php echo esc_attr($ding_configured ? '已经配置；留空表示不修改' : '粘贴 SEC 开头的加签密钥'); ?>">
                    </td>
                </tr>
                <tr>
                    <th scope="row">启用通知</th>
                    <td><label><input name="enabled" type="checkbox" value="1" <?php checked($enabled); ?>> 客户询盘成功后立即通知</label></td>
                </tr>
            </table>
            <?php submit_button('保存设置'); ?>
        </form>

        <hr>
        <form method="post" action="<?php echo esc_url(admin_url('admin-post.php')); ?>">
            <input type="hidden" name="action" value="fpcfab_tg_test">
            <?php wp_nonce_field('fpcfab_tg_test'); ?>
            <?php submit_button('发送测试消息', 'secondary'); ?>
        </form>
    </div>
    <?php
}

add_action('admin_post_fpcfab_tg_save', 'fpcfab_tg_save_settings');

function fpcfab_tg_save_settings() {
    if (!current_user_can('manage_options')) {
        wp_die('无权操作。');
    }
    check_admin_referer('fpcfab_tg_save');
    $token = isset($_POST['bot_token']) ? sanitize_text_field(wp_unslash($_POST['bot_token'])) : '';
    $chat_id = isset($_POST['chat_id']) ? preg_replace('/[^0-9-]/', '', wp_unslash($_POST['chat_id'])) : '';
    $ding_webhook = isset($_POST['ding_webhook']) ? esc_url_raw(wp_unslash($_POST['ding_webhook'])) : '';
    $ding_secret = isset($_POST['ding_secret']) ? sanitize_text_field(wp_unslash($_POST['ding_secret'])) : '';
    if ($token !== '') {
        update_option(FPCFAB_TG_TOKEN_OPTION, $token, false);
    }
    if ($chat_id !== '') {
        update_option(FPCFAB_TG_CHAT_OPTION, $chat_id, false);
    }
    if ($ding_webhook !== '') {
        update_option(FPCFAB_DING_WEBHOOK_OPTION, $ding_webhook, false);
    }
    if ($ding_secret !== '') {
        update_option(FPCFAB_DING_SECRET_OPTION, $ding_secret, false);
    }
    update_option(FPCFAB_TG_ENABLED_OPTION, isset($_POST['enabled']) ? '1' : '0', false);
    wp_safe_redirect(add_query_arg('fpcfab_tg_notice', 'saved', admin_url('admin.php?page=fpcfab-telegram-inquiry')));
    exit;
}

add_action('admin_post_fpcfab_tg_test', 'fpcfab_tg_test');

function fpcfab_tg_test() {
    if (!current_user_can('manage_options')) {
        wp_die('无权操作。');
    }
    check_admin_referer('fpcfab_tg_test');
    $message = "✅ FPCFAB 询盘通知测试成功\n时间：" . current_time('Y-m-d H:i:s');
    $ok = fpcfab_notify_send($message);
    wp_safe_redirect(add_query_arg(
        'fpcfab_tg_notice',
        $ok ? 'test_ok' : 'test_failed',
        admin_url('admin.php?page=fpcfab-telegram-inquiry')
    ));
    exit;
}

function fpcfab_tg_send($message) {
    $token = trim((string) get_option(FPCFAB_TG_TOKEN_OPTION, ''));
    $chat_id = trim((string) get_option(FPCFAB_TG_CHAT_OPTION, ''));
    if ($token === '' || $chat_id === '') {
        return false;
    }
    $response = wp_remote_post(
        'https://api.telegram.org/bot' . $token . '/sendMessage',
        array(
            'timeout' => 12,
            'redirection' => 0,
            'sslverify' => true,
            'body' => array(
                'chat_id' => $chat_id,
                'text' => fpcfab_tg_clip($message, 3800),
                'disable_web_page_preview' => 'true',
            ),
        )
    );
    if (is_wp_error($response)) {
        return false;
    }
    $body = json_decode(wp_remote_retrieve_body($response), true);
    return wp_remote_retrieve_response_code($response) === 200 && !empty($body['ok']);
}

function fpcfab_ding_send($message) {
    $webhook = trim((string) get_option(FPCFAB_DING_WEBHOOK_OPTION, ''));
    $secret = trim((string) get_option(FPCFAB_DING_SECRET_OPTION, ''));
    if ($webhook === '' || $secret === '') {
        return false;
    }
    $timestamp = (string) round(microtime(true) * 1000);
    $signature = rawurlencode(base64_encode(hash_hmac('sha256', $timestamp . "\n" . $secret, $secret, true)));
    $url = $webhook . (strpos($webhook, '?') === false ? '?' : '&') . 'timestamp=' . $timestamp . '&sign=' . $signature;
    $response = wp_remote_post(
        $url,
        array(
            'timeout' => 12,
            'redirection' => 0,
            'sslverify' => true,
            'headers' => array('Content-Type' => 'application/json; charset=utf-8'),
            'body' => wp_json_encode(array(
                'msgtype' => 'text',
                'text' => array('content' => fpcfab_tg_clip($message, 3800)),
            ), JSON_UNESCAPED_UNICODE),
        )
    );
    if (is_wp_error($response)) {
        return false;
    }
    $body = json_decode(wp_remote_retrieve_body($response), true);
    return wp_remote_retrieve_response_code($response) === 200 && isset($body['errcode']) && (int) $body['errcode'] === 0;
}

function fpcfab_notify_send($message) {
    $telegram_ok = fpcfab_tg_send($message);
    $dingtalk_ok = fpcfab_ding_send($message);
    return $telegram_ok || $dingtalk_ok;
}

function fpcfab_tg_clip($text, $limit) {
    $text = trim(wp_strip_all_tags((string) $text));
    if (function_exists('mb_strlen') && function_exists('mb_substr')) {
        return mb_strlen($text, 'UTF-8') > $limit
            ? mb_substr($text, 0, $limit - 10, 'UTF-8') . "\n……内容过长"
            : $text;
    }
    return strlen($text) > $limit ? substr($text, 0, $limit - 10) . "\n..." : $text;
}

function fpcfab_tg_value($posted, $key, $limit = 600) {
    if (!isset($posted[$key])) {
        return '';
    }
    $value = $posted[$key];
    if (is_array($value)) {
        $value = implode(', ', array_map('sanitize_text_field', $value));
    } else {
        $value = sanitize_textarea_field((string) $value);
    }
    return fpcfab_tg_clip($value, $limit);
}

function fpcfab_tg_attachment_names($uploaded_files) {
    $names = array();
    $iterator = new RecursiveIteratorIterator(new RecursiveArrayIterator((array) $uploaded_files));
    foreach ($iterator as $path) {
        if (is_string($path) && $path !== '') {
            $names[] = sanitize_file_name(wp_basename($path));
        }
    }
    return implode(', ', array_unique(array_filter($names)));
}

add_action('wpcf7_mail_sent', 'fpcfab_tg_on_mail_sent', 10, 1);

function fpcfab_tg_on_mail_sent($contact_form) {
    if (get_option(FPCFAB_TG_ENABLED_OPTION, '1') !== '1') {
        return;
    }
    if (!class_exists('WPCF7_Submission')) {
        return;
    }
    $submission = WPCF7_Submission::get_instance();
    if (!$submission) {
        return;
    }
    $posted = (array) $submission->get_posted_data();
    $field_map = array(
        'your-name' => '客户姓名',
        'your-email' => '商务邮箱',
        'your-company' => '公司名称',
        'your-phone' => '电话 / WhatsApp',
        'your-country' => '国家 / 地区',
        'product-type' => '所需服务',
        'quantity' => '预计数量',
        'requirements' => '材料 / 层数要求',
        'your-subject' => '主题',
        'your-message' => '项目详情',
    );
    $lines = array(
        '📨 FPCFAB 收到新询盘',
        '',
        '表单：' . sanitize_text_field($contact_form->title()),
    );
    foreach ($field_map as $key => $label) {
        $value = fpcfab_tg_value($posted, $key, $key === 'your-message' ? 900 : 500);
        if ($value !== '') {
            $lines[] = $label . '：' . $value;
        }
    }
    $attachments = fpcfab_tg_attachment_names($submission->uploaded_files());
    if ($attachments !== '') {
        $lines[] = '附件：' . fpcfab_tg_clip($attachments, 500) . '（文件仍保存在邮件/网站流程中）';
    }
    $source_url = sanitize_url((string) $submission->get_meta('url'));
    if ($source_url !== '') {
        $lines[] = '来源页面：' . $source_url;
    }
    $lines[] = '提交时间：' . current_time('Y-m-d H:i:s');
    $lines[] = '完整记录：' . admin_url('admin.php?page=flamingo_inbound');
    fpcfab_notify_send(implode("\n", $lines));
}
