import hashlib
import hmac
import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import re

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseServerError
from django.shortcuts import get_object_or_404, render
from django.utils.decorators import method_decorator
from django.views.generic import TemplateView
from django.views.decorators.csrf import csrf_exempt
from rest_framework.response import Response
from rest_framework.views import APIView

from ai.prompts import build_system_prompt
from ai.services import call_llm, is_out_of_scope, is_prompt_injection, retrieve
from chat.models import Conversation, Message, QuestionSansReponse

from .serializers import ChatRequestSerializer, ChatResponseSerializer

_ARABIC_RE = re.compile(r'[\u0600-\u06FF]')
logger = logging.getLogger(__name__)


class IndexView(TemplateView):
    template_name = "index.html"


def _detecter_langue(texte: str) -> str:
    """Détection très simple fr/ar par présence de caractères arabes.
    Suffisant pour le MVP (Étape 10) ; à remplacer par une détection plus
    robuste si des mélanges de langues posent problème en pratique.
    """
    return 'ar' if _ARABIC_RE.search(texte or '') else 'fr'


def _reponse_salutation(message: str, langue: str) -> str | None:
    texte = re.sub(r'[؟?!،,。.]+', ' ', message.lower()).strip()
    salutations = {
        'ar': ('السلام عليكم', 'مرحبا', 'مرحباً', 'أهلا', 'أهلًا'),
        'fr': ('bonjour', 'bonsoir', 'salut'),
    }
    if any(texte.startswith(salutation) for salutation in salutations[langue]):
        if langue == 'ar':
            return 'وعليكم السلام ورحمة الله وبركاته! كيف يمكنني مساعدتك؟'
        return 'Bonjour ! Comment puis-je vous aider ?'
    return None


def _reponse_conversationnelle(message: str, langue: str) -> str | None:
    texte = re.sub(r'[؟?!،,。.]+', ' ', message.lower()).strip()
    if langue == 'ar':
        if any(texte.startswith(prefix) for prefix in ('من أنت', 'من انت', 'أريد معلومات عنك', 'اريد معلومات عنك')):
            return (
                'أنا MPG Assistant، المساعد الرقمي الرسمي لمدرسة التعليم التقني والتكوين المهني '
                'في مجال المعادن والنفط والغاز (EETFP-MPG). أساعدك في معرفة التخصصات، '
                'القبول، التسجيل، وبرامج التكوين اعتمادًا على وثائق المدرسة.'
            )
        if any(word in texte for word in ('غبي', 'أحمق', 'حمار', 'لا تفهم')):
            return 'أفهم أن الرد لم يكن مفيدًا. أخبرني بما تحتاجه تحديدًا وسأحاول مساعدتك بدقة.'
    return None


class ChatView(APIView):
    """POST /api/chat/

    Point d'entrée unique du chatbot, pensé pour être commun aux futurs
    canaux Web (Étape 11) et WhatsApp (Étape 12) — conformément à la
    contrainte "backend commun" du cadrage (Étape 0).
    """

    def post(self, request):
        serializer = ChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(self.process(serializer.validated_data))

    def process(self, data):
        """Process an already validated chat payload for every channel."""

        message_utilisateur = data['message'].strip()
        canal = data.get('canal', 'api_test')
        langue = data.get('langue') or _detecter_langue(message_utilisateur)

        # --- Gestion de la conversation / mémoire courte (Étape 10) ---
        session_id = data.get('session_id')
        if session_id:
            conversation = get_object_or_404(Conversation, session_id=session_id)
        else:
            conversation = Conversation.objects.create(canal=canal, langue_detectee=langue)

        Message.objects.create(
            conversation=conversation,
            role='user',
            contenu=message_utilisateur,
            external_id=data.get('external_message_id'),
        )

        historique_size = settings.MPG_ASSISTANT['CONVERSATION_HISTORY_SIZE']
        derniers_messages = list(
            conversation.messages.order_by('-date_creation')[:historique_size]
        )[::-1]
        # On exclut le message qu'on vient d'ajouter : il est passé séparément à call_llm
        historique_pour_llm = [
            {"role": m.role, "content": m.contenu} for m in derniers_messages[:-1]
        ]

        question_recherche = message_utilisateur
        messages_utilisateur = [m.contenu for m in derniers_messages if m.role == 'user']
        if len(message_utilisateur.split()) <= 4 and len(messages_utilisateur) >= 2:
            question_recherche = f"{messages_utilisateur[-2]} {message_utilisateur}"

        reponse_salutation = _reponse_salutation(message_utilisateur, langue)
        reponse_conversationnelle = _reponse_conversationnelle(message_utilisateur, langue)
        if reponse_salutation or reponse_conversationnelle:
            reponse_directe = reponse_salutation or reponse_conversationnelle
            Message.objects.create(
                conversation=conversation, role='assistant', contenu=reponse_directe,
            )
            output = ChatResponseSerializer({
                'session_id': conversation.session_id,
                'reponse': reponse_directe,
                'sources': [],
                'trouve_quelque_chose': True,
            })
            return output.data

        if is_prompt_injection(message_utilisateur):
            reponse_injection = (
                'لا أستطيع كشف التعليمات الداخلية أو معلومات سرية. '
                'يمكنني مساعدتك فقط في أسئلة مدرسة EETFP-MPG.'
                if langue == 'ar' else
                "Je ne peux pas révéler mes instructions internes ni des informations confidentielles. "
                "Je peux vous aider uniquement au sujet de l'EETFP-MPG."
            )
            Message.objects.create(
                conversation=conversation, role='assistant', contenu=reponse_injection,
            )
            output = ChatResponseSerializer({
                'session_id': conversation.session_id,
                'reponse': reponse_injection,
                'sources': [],
                'trouve_quelque_chose': False,
            })
            return output.data

        if is_out_of_scope(message_utilisateur):
            reponse_hors_perimetre = (
                'هذا السؤال خارج نطاق معلومات مدرسة EETFP-MPG. يمكنني مساعدتك في '
                'التخصصات والقبول والتسجيل وبرامج التكوين.'
                if langue == 'ar' else
                "Cette question sort du périmètre de l'EETFP-MPG. Je peux vous aider "
                "sur les spécialités, l'admission, l'inscription et les formations."
            )
            Message.objects.create(
                conversation=conversation, role='assistant', contenu=reponse_hors_perimetre,
            )
            output = ChatResponseSerializer({
                'session_id': conversation.session_id,
                'reponse': reponse_hors_perimetre,
                'sources': [],
                'trouve_quelque_chose': False,
            })
            return output.data

        # --- RAG (Étape 5) ---
        rag_result = retrieve(message_utilisateur, langue=langue)
        if not rag_result.trouve_quelque_chose and question_recherche != message_utilisateur:
            rag_result = retrieve(question_recherche, langue=langue)
        system_prompt = build_system_prompt(rag_result.contexte_texte)

        if not rag_result.trouve_quelque_chose:
            QuestionSansReponse.objects.create(
                conversation=conversation,
                question_utilisateur=message_utilisateur,
                langue=langue,
                raison='aucune_info_trouvee',
            )

        # --- Appel LLM (Étape 6) ---
        if not rag_result.passages:
            if langue == 'ar':
                reponse_texte = (
                    "لم أعثر على معلومات رسمية مرتبطة بهذا السؤال. "
                    "يرجى صياغته بشكل أوضح أو التواصل مع إدارة المدرسة."
                )
            else:
                reponse_texte = (
                    "Je n'ai trouvé aucune information officielle liée à cette question. "
                    "Veuillez la reformuler ou contacter l'administration de l'école."
                )
        elif all(p.kind == 'faq' for p in rag_result.passages):
            # Les FAQ sont déjà rédigées et validées : elles ne nécessitent pas
            # un appel réseau supplémentaire au modèle externe.
            reponse_texte = "\n\n".join(p.contenu for p in rag_result.passages)
        else:
            try:
                reponse_texte = call_llm(system_prompt, historique_pour_llm, message_utilisateur)
            except RuntimeError:
                if langue == 'ar':
                    reponse_texte = (
                        "تعذر تشغيل مولد الإجابة حاليًا، لكن هذه هي المعلومات الرسمية المتاحة:\n\n"
                        + (rag_result.contexte_texte or "لم أعثر على معلومات مرتبطة بهذا السؤال.")
                    )
                else:
                    reponse_texte = (
                        "Le générateur de réponse n'est pas disponible actuellement. "
                        "Voici toutefois les informations officielles trouvées :\n\n"
                        + (rag_result.contexte_texte or "Aucune information pertinente trouvée.")
                    )

        Message.objects.create(
            conversation=conversation, role='assistant', contenu=reponse_texte,
            sources_utilisees=rag_result.sources,
        )

        output = ChatResponseSerializer({
            'session_id': conversation.session_id,
            'reponse': reponse_texte,
            'sources': rag_result.sources,
            'trouve_quelque_chose': rag_result.trouve_quelque_chose,
        })
        return output.data


# --- WhatsApp (Étape 12) : parcours guidé type « choix de langue + menu » ---
# Chaque entrée du menu pointe vers le libellé exact d'une FAQ validée, ce qui
# garantit une correspondance exacte dans retrieve() (score 1.0).
WHATSAPP_INVITE_LANGUE = 'يرجى اختيار لغتك المفضلة:\nVeuillez choisir votre langue :'
WHATSAPP_BOUTONS_LANGUE = [('lang_ar', 'العربية'), ('lang_fr', 'Français')]
WHATSAPP_TEXTES = {
    'ar': {
        'bienvenue': (
            'مرحبًا بك في المساعد الرقمي لمدرسة EETFP-MPG 🎓\n'
            'اختر موضوعًا من القائمة، أو اكتب سؤالك مباشرة.'
        ),
        'bouton_menu': 'عرض الخيارات',
        'pied': '\n\n—\nاكتب 0 للقائمة الرئيسية، أو 00 لتغيير اللغة.',
    },
    'fr': {
        'bienvenue': (
            "Bienvenue sur l'assistant numérique de l'EETFP-MPG 🎓\n"
            'Choisissez un sujet dans le menu, ou posez directement votre question.'
        ),
        'bouton_menu': 'Voir les options',
        'pied': '\n\n—\nTapez 0 pour le menu principal, ou 00 pour changer de langue.',
    },
}
WHATSAPP_MENU = [
    ({'ar': 'المدرسة', 'fr': "L'école"}, [
        ('menu_ecole', {
            'ar': ('تعريف بالمدرسة', 'ما هي مدرسة MPG؟'),
            'fr': ("Présentation de l'école", 'Qu’est-ce que l’école MPG ?'),
        }),
        ('menu_localisation', {
            'ar': ('موقع المدرسة', 'أين تقع المدرسة بالضبط وكيف أصل إليها؟'),
            'fr': ('Localisation', 'Où se trouve exactement l’école ?'),
        }),
        ('menu_contact', {
            'ar': ('التواصل مع المدرسة', 'ما هو رقم هاتف المدرسة أو بريدها الإلكتروني؟'),
            'fr': ("Contacter l'école", 'Quel est le téléphone ou l’e-mail de l’école ?'),
        }),
    ]),
    ({'ar': 'التسجيل', 'fr': 'Inscription'}, [
        ('menu_admission', {
            'ar': ('شروط القبول', 'ما هي شروط القبول في المدرسة؟'),
            'fr': ("Conditions d'admission", 'Quelles sont les conditions d’admission ?'),
        }),
        ('menu_dossier', {
            'ar': ('ملف الترشح', 'ما هي وثائق ملف الترشح؟'),
            'fr': ('Dossier de candidature', 'Que contient le dossier de candidature ?'),
        }),
        ('menu_dates', {
            'ar': ('مواعيد التسجيل', 'متى تفتح التسجيلات؟'),
            'fr': ("Dates d'inscription", 'Quand ouvrent les inscriptions ?'),
        }),
    ]),
    ({'ar': 'التكوين', 'fr': 'Formation'}, [
        ('menu_filieres', {
            'ar': ('الشعب المتوفرة', 'كم عدد الشعب في المدرسة؟'),
            'fr': ('Filières', 'Combien de filières propose l’école ?'),
        }),
        ('menu_duree', {
            'ar': ('مدة الدراسة', 'كم تدوم الدراسة؟'),
            'fr': ('Durée de la formation', 'Combien de temps dure la formation ?'),
        }),
        ('menu_examens', {
            'ar': ('التقييم والامتحانات', 'كيف يتم تقييمي؟'),
            'fr': ('Évaluations et examens', 'Comment suis-je évalué ?'),
        }),
        ('menu_stages', {
            'ar': ('التربص', 'هل التربص إجباري؟'),
            'fr': ('Stages', 'Le stage est-il obligatoire ?'),
        }),
    ]),
]
WHATSAPP_QUESTIONS_MENU = {
    row_id: libelles for _, rows in WHATSAPP_MENU for row_id, libelles in rows
}
WHATSAPP_COMMANDES_MENU = {'0', 'menu', 'القائمة'}
WHATSAPP_COMMANDES_LANGUE = {'00', 'langue', 'language', 'اللغة', 'لغة'}


def _send_whatsapp_payload(recipient: str, message: dict) -> None:
    config = settings.MPG_ASSISTANT
    access_token = config['WHATSAPP_ACCESS_TOKEN']
    phone_number_id = config['WHATSAPP_PHONE_NUMBER_ID']
    if not access_token or not phone_number_id:
        raise RuntimeError('WhatsApp Cloud API credentials are not configured.')

    url = (
        f"https://graph.facebook.com/{config['WHATSAPP_API_VERSION']}/"
        f"{phone_number_id}/messages"
    )
    payload = {
        'messaging_product': 'whatsapp',
        'recipient_type': 'individual',
        'to': recipient,
        **message,
    }
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )
    try:
        with urlopen(request, timeout=20):
            return
    except HTTPError as exc:
        details = exc.read().decode('utf-8', errors='replace')[:500]
        raise RuntimeError(f'WhatsApp Cloud API returned HTTP {exc.code}: {details}') from exc
    except URLError as exc:
        raise RuntimeError('Could not reach WhatsApp Cloud API.') from exc


def send_whatsapp_message(recipient: str, text: str) -> None:
    _send_whatsapp_payload(recipient, {
        'type': 'text',
        'text': {'preview_url': False, 'body': text[:4096]},
    })


def send_whatsapp_language_choice(recipient: str) -> None:
    _send_whatsapp_payload(recipient, {
        'type': 'interactive',
        'interactive': {
            'type': 'button',
            'body': {'text': WHATSAPP_INVITE_LANGUE},
            'action': {'buttons': [
                {'type': 'reply', 'reply': {'id': button_id, 'title': title}}
                for button_id, title in WHATSAPP_BOUTONS_LANGUE
            ]},
        },
    })


def send_whatsapp_menu(recipient: str, langue: str, body: str | None = None) -> None:
    textes = WHATSAPP_TEXTES[langue]
    _send_whatsapp_payload(recipient, {
        'type': 'interactive',
        'interactive': {
            'type': 'list',
            'body': {'text': (body or textes['bienvenue'])[:1024]},
            'action': {
                'button': textes['bouton_menu'],
                'sections': [
                    {
                        'title': titres[langue],
                        'rows': [
                            {'id': row_id, 'title': libelles[langue][0]}
                            for row_id, libelles in rows
                        ],
                    }
                    for titres, rows in WHATSAPP_MENU
                ],
            },
        },
    })


def _whatsapp_choix(inbound: dict) -> tuple[str, str]:
    """Retourne (identifiant, libellé) d'une réponse à un bouton ou à une liste."""
    interactive = inbound.get('interactive', {})
    reply = interactive.get('button_reply') or interactive.get('list_reply') or {}
    return reply.get('id', ''), reply.get('title', '')


@method_decorator(csrf_exempt, name='dispatch')
class WhatsAppWebhookView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        config = settings.MPG_ASSISTANT
        if (
            request.GET.get('hub.mode') == 'subscribe'
            and hmac.compare_digest(
                request.GET.get('hub.verify_token', ''), config['WHATSAPP_VERIFY_TOKEN'],
            )
            and config['WHATSAPP_VERIFY_TOKEN']
        ):
            return HttpResponse(request.GET.get('hub.challenge', ''))
        return HttpResponseForbidden('Webhook verification failed.')

    def post(self, request):
        config = settings.MPG_ASSISTANT
        app_secret = config['WHATSAPP_APP_SECRET']
        signature = request.headers.get('X-Hub-Signature-256', '')
        expected_signature = 'sha256=' + hmac.new(
            app_secret.encode('utf-8'), request.body, hashlib.sha256,
        ).hexdigest() if app_secret else ''
        if not app_secret or not hmac.compare_digest(signature, expected_signature):
            return HttpResponseForbidden('Invalid webhook signature.')

        try:
            payload = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return HttpResponse('Invalid JSON.', status=400)

        for entry in payload.get('entry', []):
            for change in entry.get('changes', []):
                value = change.get('value', {})
                for inbound in value.get('messages', []):
                    try:
                        self.handle_message(inbound)
                    except Exception:
                        logger.exception('Failed to process an incoming WhatsApp message.')
                        return HttpResponseServerError('Message processing failed.')

        return HttpResponse('EVENT_RECEIVED')

    def handle_message(self, inbound: dict) -> None:
        sender = inbound.get('from')
        message_id = inbound.get('id')
        message_type = inbound.get('type')
        if not sender or not message_id or message_type not in ('text', 'interactive'):
            return

        if message_type == 'text':
            text = inbound.get('text', {}).get('body', '').strip()
            choix_id, choix_titre = '', ''
        else:
            choix_id, choix_titre = _whatsapp_choix(inbound)
            text = ''
        if not text and not choix_id:
            return

        # Meta renvoie un événement non acquitté : on ne le traite qu'une fois,
        # mais on renvoie la réponse déjà produite au cas où l'envoi avait échoué.
        previous_message = Message.objects.filter(
            external_id=message_id, role='user',
        ).select_related('conversation').first()
        if previous_message:
            next_message = previous_message.conversation.messages.filter(
                date_creation__gte=previous_message.date_creation,
            ).exclude(pk=previous_message.pk).order_by('date_creation').first()
            if next_message and next_message.role == 'assistant':
                send_whatsapp_message(sender, next_message.contenu)
            return

        # langue_detectee vide = l'utilisateur n'a pas encore choisi sa langue.
        conversation, created = Conversation.objects.get_or_create(
            whatsapp_id=sender,
            defaults={'canal': 'whatsapp', 'langue_detectee': ''},
        )
        langue_choisie = conversation.langue_detectee

        def enregistrer_action(contenu: str) -> None:
            Message.objects.create(
                conversation=conversation, role='user', contenu=contenu,
                external_id=message_id,
            )

        commande = text.lower()
        if choix_id in ('lang_ar', 'lang_fr'):
            enregistrer_action(choix_titre or choix_id)
            conversation.langue_detectee = choix_id.removeprefix('lang_')
            conversation.save(update_fields=['langue_detectee', 'date_derniere_activite'])
            send_whatsapp_menu(sender, conversation.langue_detectee)
            return

        if not langue_choisie or commande in WHATSAPP_COMMANDES_LANGUE:
            enregistrer_action(text or choix_titre or choix_id)
            send_whatsapp_language_choice(sender)
            return

        if commande in WHATSAPP_COMMANDES_MENU:
            enregistrer_action(text)
            send_whatsapp_menu(sender, langue_choisie)
            return

        if choix_id in WHATSAPP_QUESTIONS_MENU:
            langue = langue_choisie
            question = WHATSAPP_QUESTIONS_MENU[choix_id][langue][1]
        elif text:
            # Une question libre reçoit une réponse dans la langue où elle est écrite.
            langue = _detecter_langue(text)
            question = text[:2000]
        else:
            return

        answer = ChatView().process({
            'message': question,
            'langue': langue,
            'canal': 'whatsapp',
            'session_id': conversation.session_id,
            'external_message_id': message_id,
        })
        send_whatsapp_message(sender, answer['reponse'] + WHATSAPP_TEXTES[langue]['pied'])
