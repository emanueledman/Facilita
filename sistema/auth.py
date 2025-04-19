import logging
import firebase_admin
from firebase_admin import auth, credentials
import json
import jwt
from django.contrib.auth.models import User
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from django.conf import settings

logger = logging.getLogger(__name__)

def initialize_firebase():
    """Inicializa o Firebase se ainda não estiver inicializado."""
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

        # Extrair token do cabeçalho
        token = auth_header.replace('Bearer ', '') if auth_header.lower().startswith('bearer ') else auth_header

        # 1. Tentar Firebase
        firebase_initialized = initialize_firebase()
        if firebase_initialized:
            try:
                decoded_token = auth.verify_id_token(token)
                uid = decoded_token.get('uid')
                email = decoded_token.get('email', f'{uid}@example.com')
                user_tipo = decoded_token.get('user_tipo', 'usuario')

                # Criar ou obter usuário no Django
                user, created = User.objects.get_or_create(
                    username=uid,
                    defaults={'email': email}
                )
                if created:
                    logger.info(f"Usuário criado para UID: {uid}")
                # Armazenar user_tipo na sessão ou como atributo do usuário
                request.user_tipo = user_tipo
                logger.info(f"Autenticado via Firebase - UID: {uid}, Tipo: {user_tipo}")
                return user, None
            except Exception as firebase_error:
                logger.warning(f"Falha na autenticação Firebase: {str(firebase_error)}")

        # 2. Tentar JWT local
        try:
            payload = jwt.decode(token, settings.JWT_SECRET, algorithms=['HS256'])
            user_id = payload.get('user_id')
            user_tipo = payload.get('user_tipo', 'usuario')
            try:
                user = User.objects.get(username=user_id)
                request.user_tipo = user_tipo
                logger.info(f"Autenticado via JWT - User ID: {user_id}, Tipo: {user_tipo}")
                return user, None
            except User.DoesNotExist:
                logger.warning(f"Usuário não encontrado para user_id: {user_id}")
                raise AuthenticationFailed('Usuário não encontrado')
        except jwt.InvalidAlgorithmError:
            logger.warning("Token JWT inválido: Algoritmo não permitido")
            raise AuthenticationFailed('Algoritmo de token inválido')
        except jwt.ExpiredSignatureError:
            logger.warning("Token JWT expirado")
            raise AuthenticationFailed('Token expirado')
        except jwt.InvalidTokenError as jwt_error:
            logger.warning(f"Token JWT inválido: {str(jwt_error)}")
            raise AuthenticationFailed('Token inválido')

        # 3. Tentar token DRF
        try:
            from rest_framework.authtoken.models import Token
            token_obj = Token.objects.get(key=token)
            user = token_obj.user
            request.user_tipo = 'usuario'  # Padrão, já que DRF não inclui user_tipo
            logger.info(f"Autenticado via token DRF - User ID: {user.id}")
            return user, None
        except Token.DoesNotExist:
            logger.warning("Token DRF inválido")
            raise AuthenticationFailed('Token inválido')
        except Exception as e:
            logger.error(f"Erro de autenticação: {str(e)}")
            raise AuthenticationFailed('Falha na autenticação')