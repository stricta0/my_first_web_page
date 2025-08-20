import streamlit as st

st.title("Moja pierwsza aplikacja w Streamlit 🎉")
st.write("To jest prosta aplikacja webowa w Pythonie.")

name = st.text_input("Jak masz na imię?")
if st.button("Powiedz cześć"):
    st.success(f"Cześć, {name}!")
