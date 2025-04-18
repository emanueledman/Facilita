import logging
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from django.conf import settings
import firebase_admin
from firebase_admin import auth, credentials
import json
import jwt
from django.contrib.auth.models import User

logger = logging.getLogger(__name__)

def initialize_firebase():
    if not firebase_admin._apps:
        try:
            firebase_creds = settings.FIREBASE_CREDENTIALS
            if isinstance(firebase_creds, str):
                try:
                    cred_dict = json.loads(firebase_creds)
                    cred = credentials.Certificate(cred_dict)
                except json.JSONDecodeError:
                    cred = credentials.Certificate(firebase_creds)
            else:
                cred = credentials.Certificate(firebase_creds)
            firebase_admin.initialize_app(cred)
            logger.info("Firebase inicializado com sucesso")
        except Exception as e:
            logger.error(f"Erro ao inicializar Firebase: {e}")
            return False
    return True

class FirebaseAndTokenAuthentication(BaseAuthentication):
    def authenticate(self, request):
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            logger.warning("Cabeçalho de autorização ausente")
            raise AuthenticationFailed('Token de autenticação necessário')

        token = auth_header.replace('Bearer ', '') if auth_header.lower().startswith('bearer ') else auth_header

        # 1. Tentar Firebase
        firebase_initialized = initialize_firebase()
        if firebase_initialized:
            try:
                decoded_token = auth.verify_id_token(token)
                uid = decoded_token.get('uid')
                user, created = User.objects.get_or_create(
                    username=uid,
                    defaults={'email': decoded_token.get('email', f'{uid}@example.com')}
                )
                perfil, _ = PerfilUsuario.objects.get_or_create(
                    usuario=user,
                    defaults={'papel_usuario': decoded_token.get('user_tipo', 'usuario')}
                )
                logger.info(f"Autenticado via Firebase - UID: {uid}")
                return user, None
            except Exception as firebase_error:
                logger.warning(f"Falha Firebase: {str(firebase_error)}")

        # 2. Tentar token DRF
        try:
            from rest_framework.authtoken.models import Token
            token_obj = Token.objects.get(key=token)
            user = token_obj.user
            logger.info(f"Autenticado via token DRF - User ID: {user.id}")
            return user, None
        except Token.DoesNotExist:
            logger.warning("Token DRF inválido")
            raise AuthenticationFailed('Token inválido')
        except Exception as e:
            logger.error(f"Erro de autenticação: {str(e)}")
            raise AuthenticationFailed('Falha na autenticação')