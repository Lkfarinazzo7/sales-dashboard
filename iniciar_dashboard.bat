@echo off
title Dashboard de Vendas

echo.
echo  =============================================
echo   DASHBOARD DE VENDAS - Meetime + Agendor
echo  =============================================
echo.

:: Verifica se Python esta instalado
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  ATENCAO: Python nao encontrado!
    echo.
    echo  Siga estes passos:
    echo   1. O site de download vai abrir agora
    echo   2. Clique no botao amarelo "Download Python"
    echo   3. Abra o arquivo que baixou
    echo   4. IMPORTANTE: marque a opcao "Add Python to PATH"
    echo   5. Clique em "Install Now"
    echo   6. Apos instalar, feche esta janela e abra o .bat novamente
    echo.
    start https://www.python.org/downloads/
    pause
    exit /b
)

echo  Python encontrado com sucesso!
echo.

:: Vai para a pasta do script
cd /d "%~dp0"

:: Instala dependencias
echo  Instalando dependencias (aguarde)...
pip install flask requests --quiet --disable-pip-version-check
echo  Dependencias instaladas!
echo.

:: Abre o navegador apos 4 segundos
echo  Abrindo o navegador em instantes...
echo  Endereco: http://localhost:5000
echo.
echo  Para fechar o dashboard, feche esta janela.
echo  =============================================
echo.

start "" cmd /c "ping -n 5 127.0.0.1 > nul & start http://localhost:5000"

:: Inicia o dashboard
python dashboard.py

echo.
echo  O dashboard foi encerrado.
pause
