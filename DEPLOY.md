# PinDown: configuração e publicação

## Senha de acesso

Antes de publicar o app na internet, configure `PINDOWN_PASSWORD` com uma senha compartilhada de pelo menos 12 caracteres e `APP_ENV=production` nas variáveis secretas do serviço. Sem `APP_ENV=development` nem uma senha forte configurada, o app não inicia; assim, uma configuração incompleta falha de forma segura. O navegador pedirá usuário `friends` e a senha compartilhada ao abrir o site.

As proteções de abuso limitam prévias e downloads por endereço IP e permitem no máximo dois downloads simultâneos por processo. Esses limites são locais ao processo: hospedagem com várias instâncias pode exigir limites no proxy ou serviço de borda também.

Se o provedor usar proxy reverso, configure `FORWARDED_ALLOW_IPS` somente com os endereços confiáveis do proxy fornecidos por ele. Não use `*` em um serviço exposto à internet: confiar em cabeçalhos enviados por qualquer cliente permitiria contornar o limite por IP.

## Requisitos do serviço

- Python 3.10 ou mais recente.
- FFmpeg instalado e disponível no `PATH`.
- HTTPS, necessário para instalação da PWA e recomendado para a senha.
- Disco temporário e memória suficientes para downloads de até 150 MB. Os arquivos temporários são removidos após o envio ao navegador.
- Comando de inicialização: `uvicorn app:app --host 0.0.0.0 --port $PORT` (use a sintaxe de variável de ambiente exigida pelo seu provedor).

O repositório inclui um `Dockerfile` que instala FFmpeg, usa usuário sem privilégios e respeita a variável `PORT`. Para testar localmente, construa a imagem e execute o contêiner definindo `PINDOWN_PASSWORD` com uma senha forte; provedores compatíveis com Docker podem usar essa mesma imagem.

Hospedagens gratuitas podem suspender serviços inativos, limitar CPU, espaço ou tráfego e bloquear requisições às plataformas. Teste cada rede no provedor escolhido antes de compartilhar o endereço.

## Conteúdo suportado

O app aceita links HTTPS do Pinterest, Instagram, YouTube e Facebook. O conteúdo precisa ser público, conter vídeo e estar acessível sem login. Respeite os direitos autorais e os termos de cada plataforma.

## Compartilhar links com o PinDown

Depois de publicar em HTTPS e instalar a PWA em um navegador compatível, use o menu **Compartilhar** do Pinterest e escolha PinDown. O link será preenchido e a busca começará automaticamente. A disponibilidade do PinDown como destino depende do navegador, do sistema operacional e da instalação da PWA; se não aparecer no menu, cole o link no app como antes.
