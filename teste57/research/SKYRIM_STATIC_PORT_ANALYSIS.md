# Skyrim Android → Spore: análise de arquitetura e próximo experimento

Analisado em 2026-11-02. Referência: https://github.com/ttom795/Skyrim_Android
Commit inspecionado: `ceb35d48467c8c2f8ee4eaab3f6671c7b63c589b`, também apontado pela tag `0.27.6` consultada. Esta análise é do código, não um teste de Skyrim em aparelho.

## Conclusão

Este projeto é uma base plausível para um experimento de recompilação estática de Spore. Não é uma engine que apenas lê assets, nem um launcher Wine/Box64. Preserva a lógica x86 por tradução para C, compilada para a CPU hospedeira, e fornece uma implementação específica das interfaces Windows e D3D9 usadas pelo jogo.

Nosso launcher atual é outra arquitetura. Resolver Wine, prefixo, ntdll e Box64 não implementa a arquitetura deste projeto. Seu código de seleção/importação de arquivos e interface pode ser reaproveitado; o caminho de execução precisará ser substituído. Manter o launcher antigo como experimento separado, sem apagá-lo.

## 1. Pipeline real

1. `tools/peimg.py`: lê PE32 e aplica relocação quando solicitada.
2. `tools/discover.py`: descoberta por percurso de fluxo, alvos de chamadas, ponteiros de código e tabelas de saltos. Capstone decodifica x86. Dados em seções executáveis podem parecer código; descoberta não equivale a prova de cobertura.
3. `analysis/functions*.json`: blocos, imports e tabelas de saltos usados pelo tradutor.
4. `tools/lift.py`: emite funções C `f_ENDERECO(CPU *c)`, arquivos de tabela de despacho e relatório de rejeições.
5. NDK/CMake: compila o código gerado junto ao runtime, SDL2 e DXVK nativo para uma biblioteca Android.
6. Runtime abre os PE originais, copia seções/dados, aplica relocação, resolve imports, inicializa estado e chama a função C correspondente ao entry point.

O APK não executa diretamente as instruções x86 contidas no PE. O PE ainda é necessário para dados, layout e metadados. Traduzir instruções não recupera os fontes C++ originais.

## 2. Estado x86 dentro do processo ARM64

`runtime/x86.h` modela registradores, flags, x87, SSE/MMX e estado da thread. Ponteiros do jogo continuam valores de 32 bits. Acesso à memória é feito a partir de `MEM + endereço_guest`, e não convertendo indiscriminadamente cada campo em ponteiro ARM64.

`runtime/main.c` reserva 4 GiB de espaço virtual com mmap/MAP_NORESERVE. Isso não representa consumo imediato de 4 GiB de RAM física. As páginas utilizadas e os demais subsistemas determinam o consumo real.

`runtime/dispatch.c` converte endereços de chamadas indiretas em funções C por tabelas e caches. Imports recebem identificadores intermediários (thunks). Um import ausente pode ser registrado, mas sua execução aborta com diagnóstico. Portanto, resolver o carregamento do PE não comprova que todas as funções necessárias estejam implementadas.

## 3. Camada Windows

Há implementações de arquivos, diretórios, heap, threads, sincronização, TLS, interface, entrada e CRT em `kernel32.c`, `user32.c`, `msvcrt.c`, `misc.c`, `dinput.c` e outros arquivos.

A fronteira real é maior que a tabela de imports: GetProcAddress, callbacks e interfaces COM podem introduzir chamadas em execução. O código deve preservar ABI x86, limpeza da pilha, layouts e distinção entre estruturas guest e host.

Limitações concretas encontradas:

- `main.c` registra aviso de que callbacks TLS não são executados. Verificar se existem e são necessários no Spore; não presumir que todo PE precise deles.
- `_CxxThrowException` e `__CxxFrameHandler` em `msvcrt.c` abortam. Não existe suporte geral a exceções C++ nessa implementação.
- Há condicionais, nomes e endereços específicos de jogos. `game.h` usa Batman como padrão; `lift.py` também seleciona overrides de Batman quando LIFT_GAME não é reconhecido. Apenas definir `LIFT_GAME=spore` atualmente é incorreto: precisa adicionar um perfil explícito.
- Estado de multithreading exige revisão: o comentário de `lift_prelude.h` e o CMake mostram acessos volatile como padrão ARM64 e opção LB_STRICT_ORDER. O README simplifica a descrição. Volatile não substitui a ordenação x86; avaliar o modo estrito antes de otimizar e testar sincronização especificamente.

## 4. Gráficos

O trajeto é: chamada D3D9 do código traduzido → ponte guest/host → dxvk-native modificado → Vulkan → apresentação SDL/Android.

`d3d9_bridge.cpp` mantém associação entre objetos COM do guest e objetos nativos. Cria vtables guest, faz a conversão dos parâmetros e trata operações que envolvem ponteiros, locks e apresentação. `d3d9_bridge_gen.cpp` contém a parte gerada. A existência de apenas Direct3DCreate9 na tabela de imports não significa que só há uma função gráfica a implementar.

Não basta copiar as DLLs DXVK usadas pelo Wine. Este projeto liga uma versão nativa e usa uma ponte própria de ABI. Há também caminhos de adaptação de formatos e texturas em `d3d9_bcemu.cpp`.

Para Spore, D3D9 é o alvo observado. Nosso teste d3d11-probe atual não comprova compatibilidade com ele.

## 5. Áudio e plataforma

SDL2 fornece janela, entrada e áudio; há implementações próprias de DirectSound e XAudio2. A rota Skyrim traduz também Bink e X3DAudio. A seleção de DLLs deve vir do Spore, não copiar a lista de Skyrim por conveniência. O README relata silêncio em áudio xWMA e desempenho baixo na Adreno 610: são limitações declaradas pelo autor, não medidas nesta sessão.

## 6. Resultado concreto no Spore

Executável analisado: SHA-256 `290703b34fe4573b242c3e271cc90568992abe6fb3fa9333a7341044a759fc74`.

Confirmei diretamente no PE 495 imports distribuídos em 20 DLLs. Destaques:

| DLL | Imports | Implicação |
|---|---:|---|
| MSVCR90.dll | 159 | CRT de versão diferente; a tabela inspecionada registra msvcrt.dll. Auditar funções e imports de dados, não fazer alias cego. |
| KERNEL32.dll | 148 | Candidato a reaproveitamento parcial; verificar semântica e cobertura. |
| USER32.dll | 89 | Janela, mensagens e callbacks precisam da implementação host. |
| WSOCK32.dll | 23 | Rede e inicialização podem alcançar funções mesmo no fluxo offline. |
| d3dx9_27.dll | 9 | Skyrim usa d3dx9_42; avaliar tradução da versão correta. |
| DINPUT8.dll | 1 | Métodos indiretos da interface não estão contados aqui. |
| DSOUND.dll | 2 | Reaproveitamento possível da implementação, sujeito a teste. |
| d3d9.dll | 1 | Interfaces COM ampliam muito a superfície. |
| steam_api.dll | 3 | Examinar a interface realmente necessária ao executável. |

As demais DLLs são WINMM, PSAPI, GDI32, ADVAPI32, SHELL32, ole32, VERSION, IMM32, USP10, dbghelp e WS2_32. A presença de código com nomes parecidos no runtime não confirma cobertura funcional.

### Experimento executado

Foi delimitada manualmente a função 0x432F10 com Capstone e gerado um JSON de blocos para o tradutor original. O tradutor reportou `lifted 1 rejected 0`. Compilei o C com GCC em Linux x86-64, usando os headers do runtime, e executei três casos:

| Entrada | EAX observado | ESP após retorno | EBP preservado |
|---|---|---|---|
| DFAD9F51 | 1D2EC0A4 | 00020004 | 12345678 |
| 00000000 | FFFFFFFF | 00020004 | 12345678 |
| FFFFFFFF | FFFFFFFF | 00020004 | 12345678 |

É evidência de tradução e execução desta função e desses caminhos. Não é descoberta automática de todo o executável, teste diferencial completo, boot do jogo, compilação ARM64 nem teste Android. Os ramos que leem globais não foram validados por esses casos. O C derivado do executável não foi publicado neste relatório.

## 7. Cobertura e validação

O tradutor pode tanto rejeitar funções quanto emitir chamadas rt_unimpl para instruções não cobertas. O comando terminar e gerar muitos arquivos não significa sucesso do port. Inspecionar `*_rejected.json` e ocorrências rt_unimpl; identificar se pertencem a código real e se são alcançáveis no caminho testado.

`tools/difftest.py` compara trechos traduzidos com Unicorn. É uma boa base, mas há uma lista explícita de instruções excluídas. Não equivale a prova completa de equivalência, nem testa automaticamente todos os efeitos de ABI e concorrência ARM64.

## 8. Caminho recomendado, com critérios objetivos

1. Fixar executável, hash e commit do tradutor. Criar perfil GAME_SPORE explícito, tabelas de módulos e caminhos próprios, sem endereços de Skyrim/Batman herdados.
2. Inventariar imports com correspondência DLL+símbolo, distinguir funções/dados/ordinais e listar DLLs originais necessárias. Obter d3dx9_27 correto e decidir quais dependências traduzir versus implementar.
3. Executar descoberta completa com monitoramento de memória/tempo. Inspecionar lacunas, saltos indiretos e rejeições; não iniciar compilação massiva antes dessa inspeção.
4. Validar instruções e funções representativas, inclusive x87/SSE, conversões numéricas e efeitos de pilha. O switch testado é só o primeiro ponto.
5. Primeiro boot Linux com o mesmo runtime: CRT → entry point → inicialização do aplicativo → leitura de um pacote real. Parar em imports ausentes com diagnóstico, nunca retornar sucesso genérico para destravar.
6. Validar criação D3D9, apresentação e um frame real do jogo. Conferir dados de textura/shader e chamadas COM; depois áudio e controles.
7. Compilar ARM64 e executar no aparelho. Comparar comportamento, threads, carregamento e memória; testar modo de ordenação estrito antes de atribuir falhas à lógica do jogo.
8. Entrar na Fase Celular, carregar/salvar e jogar; só então otimizar resolução, shaders, carregamento e funções quentes por medição.

Não é realista traduzir apenas as funções com nomes ou IDs celulares e ignorar o restante: CRT, gerência de recursos, interface e gráficos são compartilhados. O primeiro objetivo funcional pode ser celular; o conjunto necessário de código será maior.

## 9. Reaproveitamento do trabalho atual

- Launcher Android: seleção de pasta, importação, apresentação de logs e opções.
- Pesquisa DBPF/PROP: diagnóstico de recursos e confirmação de carregamento; um port estático pode manter o parser original traduzido.
- Box64/Wine: experimento de compatibilidade separado, não dependência obrigatória desta arquitetura.
- Código Java do protótipo: não substitui a lógica recompilada do Spore.

A prioridade deve sair de corrigir o boot Wine como requisito do produto e passar para medir cobertura do tradutor e do runtime para Spore. Nenhum APK Spore recompilado foi produzido nesta análise.

## Fontes auditáveis

Base fixa para todos os caminhos abaixo:
https://github.com/ttom795/Skyrim_Android/tree/ceb35d48467c8c2f8ee4eaab3f6671c7b63c589b

- tools/regen_gen.sh, discover.py, lift.py, peimg.py, difftest.py
- runtime/main.c, x86.h, dispatch.c, shims.c, modules.c, game.h
- runtime/lift_prelude.h, kernel32.c, msvcrt.c
- runtime/d3d9_bridge.cpp, d3d9_bridge_gen.cpp, d3d9_bcemu.cpp
- runtime/platform_sdl.c, dsound.c, xaudio2.c, touch.cpp
- android/app/jni/game/CMakeLists.txt, README.md
