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


def send_whatsapp_message(recipient: str, text: str) -> None:
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
        'type': 'text',
        'text': {'preview_url': False, 'body': text[:4096]},
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
        raise RuntimeError(f'WhatsApp Cloud API returned HTTP {exc.code}.') from exc
    except URLError as exc:
        raise RuntimeError('Could not reach WhatsApp Cloud API.') from exc


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
                    if inbound.get('type') != 'text':
                        continue
                    sender = inbound.get('from')
                    message_id = inbound.get('id')
                    text = inbound.get('text', {}).get('body', '').strip()
                    if not sender or not message_id or not text:
                        continue

                    try:
                        conversation, _ = Conversation.objects.get_or_create(
                            whatsapp_id=sender,
                            defaults={
                                'canal': 'whatsapp',
                                'langue_detectee': _detecter_langue(text),
                            },
                        )
                        previous_message = Message.objects.filter(
                            external_id=message_id, role='user',
                        ).select_related('conversation').first()
                        if previous_message:
                            previous_answer = previous_message.conversation.messages.filter(
                                role='assistant',
                                date_creation__gte=previous_message.date_creation,
                            ).first()
                            if previous_answer:
                                send_whatsapp_message(sender, previous_answer.contenu)
                            continue

                        answer = ChatView().process({
                            'message': text[:2000],
                            'canal': 'whatsapp',
                            'session_id': conversation.session_id,
                            'external_message_id': message_id,
                        })
                        send_whatsapp_message(sender, answer['reponse'])
                    except Exception:
                        logger.exception('Failed to process an incoming WhatsApp message.')
                        return HttpResponseServerError('Message processing failed.')

        return HttpResponse('EVENT_RECEIVED')
